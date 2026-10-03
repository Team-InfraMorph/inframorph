#!/usr/bin/env python3
"""Generate isolated, read-only evidence examples with actual old/new checkers.

No source copies are committed. The frozen baseline is extracted from Git to a
temporary directory and evaluated in a separate process. AI candidates default
to explicitly labelled replay; --openai uses the existing bounded API backend.
"""
import argparse
import asyncio
from contextlib import asynccontextmanager
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
# The integrated 1.0.0 freeze matches baselines/1.0.0.json's recorded digest.
# Its older source_commit field predates the integration/document-version cleanup.
# Verify the actual checker identity below; never relabel a current result.
BASELINE_COMMIT = 'cf987c3c8e87a856a3d3e46ead457ec3754df0bd'
sys.path.insert(0, str(ROOT))
from analyzer.backend import OpenAIBackend, ReplayBackend, Reply
from analyzer.config import Limits
from analyzer.redaction import Redactor
from analyzer.snapshot import Snapshot
from analyzer.source_policy import validate_demo_intent
from control_plane.auto_repair import repair, history, RepairStopped
from control_plane.db import Store
from control_plane.policy_results import check, read
from control_plane.policy_lifecycle import details, enqueue, run_next, comparison
from policy_gate.catalog import identity, release, fingerprint, canonical
from policy_gate.gate import validate_intent
from policy_gate.reporting import now
from schemas import Intent, RepoMap


# This program is imported ONLY in the chosen checkout's separate interpreter.
# The application under inspection is read as data, never imported/executed.
CHECKER = r'''
import json,sys,tempfile
from pathlib import Path
from schemas import Intent,Plan,RepoMap
from analyzer.source_policy import validate_demo_intent,validate_demo_plan
from policy_gate.gate import validate_intent,validate_plan,validate_patch
from policy_gate.catalog import identity,release
from code_patch.runner import patch_snapshot
from builder.runtime import check_build_profile
from control_plane.db import Store
from control_plane.policy_results import check,read
from control_plane.policy_lifecycle import details
data=json.load(sys.stdin)
fixture=Path(data['fixture'])
source=fixture/'tests/fixtures/analyzer/v2/snapshot'
mapping=RepoMap.model_validate_json((fixture/'tests/fixtures/analyzer/v2/repo_map.json').read_text())
intent=Intent.model_validate(data['intent'])
plan=Plan.model_validate_json((fixture/'schemas/fixtures/v2/plan.local.json').read_text())
rows=[]
decision,code='PASS','passed'
with tempfile.TemporaryDirectory() as tmp:
 store=Store(Path(tmp).resolve()/'checks.db')
 project=store.create_project('https://github.com/Team-InfraMorph/demo-app','main',['local'])
 did=store.begin_deploy(project['project_id'])
 store.set_commit(did,mapping.commit)
 def run(stage,action):return check(store,did,'local',stage,action)
 try:
  run('source',lambda:validate_demo_intent(intent,source,mapping))
  run('intent',lambda:validate_intent(intent,source,mapping.commit))
  run('profile',lambda:validate_demo_plan(plan,mapping))
  run('plan',lambda:validate_plan(intent,plan))
  patch_snapshot(source,mapping,plan,Path(tmp).resolve()/'patch')
  artifact=run('patch',lambda:validate_patch(source,Path(tmp).resolve()/'patch',plan))
  run('build_profile',lambda:check_build_profile(artifact.files))
 except Exception:
  rows=read(store,did)['results']
  if not rows or rows[-1]['decision']=='PASS':raise
  decision,code=rows[-1]['decision'],rows[-1]['reason_code']
 rows=read(store,did)['results']
 events=details(store,did)
 store.close()
json.dump(dict(identity=identity(),release=release(),results=rows,history=events,decision=decision,reason_code=code),sys.stdout)
'''


def inspect(checkout, value):
    result = subprocess.run([sys.executable, '-c', CHECKER], cwd=checkout,
        input=json.dumps(dict(fixture=str(ROOT), intent=value)), text=True,
        capture_output=True, check=True, timeout=120,
        env={**os.environ, 'PYTHONPATH':str(checkout)})
    return json.loads(result.stdout)


def rows_for_ui(rows, *, update=False, offset=0):
    return [dict(row, seq=offset+i+1, target='local', attempt=0,
                 purpose='policy_update' if update else 'deployment') for i,row in enumerate(rows)]


async def repair_example(store, raw, good, name, use_openai):
    source = ROOT/'tests/fixtures/analyzer/v2/snapshot'
    mapping = RepoMap.model_validate_json((ROOT/'tests/fixtures/analyzer/v2/repo_map.json').read_text())
    project = store.create_project('https://github.com/Team-InfraMorph/demo-app','main',['local'])
    did = store.begin_deploy(project['project_id'])
    store.set_commit(did,mapping.commit)
    limits = Limits(timeout_seconds=90, max_estimated_usd=.1)
    digest = Snapshot(source,mapping.tree,limits,Redactor()).digest
    expected = raw if name=='repair-exhausted' else good
    # Exhaustion is deterministic replay; a real model is never forced to fail
    # and its actual response is never replaced with a saved successful answer.
    actual = use_openai and name!='repair-exhausted'
    backend = OpenAIBackend() if actual else ReplayBackend([Reply(text=json.dumps(expected))]*3)
    @asynccontextmanager
    async def session():
        yield backend
    def gate(value,attempt=0):
        check(store,did,'local','source',lambda:validate_demo_intent(value,source,mapping),attempt=attempt)
        check(store,did,'local','intent',lambda:validate_intent(value,source,mapping.commit),attempt=attempt)
    stop = None
    try:
        initial = Intent.model_validate(raw)
        try:
            gate(initial)
        except ValueError as error:
            try:
                await repair(store=store,did=did,mapping=mapping,source=source,stage='intent',target='local',
                    initial=initial,error=error,validate=gate,session=session,limits=limits,
                    initial_metrics={'snapshot_digest':digest,'usage_complete':True})
            except RepairStopped as stopped:
                stop = str(stopped)
        else:
            raise ValueError('example_failure_not_reproduced')
    finally:
        if actual:
            await backend.close()
        store.set_status(did,'FAILED')  # No deployment or running service was verified.
    return dict(policy_data=read(store,did), history=details(store,did),
                ai_backend=backend.name, stop_reason=stop, deployment_id=did)


async def generate(args):
    output = args.output_dir.absolute()
    if any(p.is_symlink() for p in (output,*output.parents)):
        raise ValueError('unsafe_output_directory')
    output.mkdir(parents=True,exist_ok=False,mode=0o700)
    baseline = json.loads((ROOT/'policy_gate/baselines/1.0.0.json').read_text())
    archive = subprocess.run(['git','archive',BASELINE_COMMIT],cwd=ROOT,
                             capture_output=True,check=True).stdout
    good = json.loads((ROOT/'schemas/fixtures/v2/intent.json').read_text())
    db = copy.deepcopy(good); db['state'][0]['evidence']=['prisma/schema.prisma:7']
    worker = copy.deepcopy(good); worker['workloads'][1]['evidence']=['src/worker.js:1']
    store = Store(output/'control_plane.db')
    items, summary = [], []
    try:
        with tempfile.TemporaryDirectory(prefix='inframorph-policy-baseline-') as directory:
            with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
                bundle.extractall(directory,filter='data')
            for name,title,value in [('direct','정상 근거 · 이전과 현재 모두 통과',good),
                                     ('db-url-only','DB URL만 인용 · 직접 선언 근거 부족',db),
                                     ('worker-initializer','worker 초기화만 인용 · 명령과 시작점 부족',worker)]:
                before,after = inspect(directory,value),inspect(ROOT,value)
                assert before['identity']['version']=='1.0.0' and before['decision']=='PASS'
                assert before['identity']['policy_digest']==baseline['identity']['policy_digest']
                assert after['decision']==('PASS' if name=='direct' else 'BLOCK')
                with store._lock,store._conn:
                    store._conn.execute('INSERT OR IGNORE INTO policy_releases VALUES (?,?,?)',
                        (before['identity']['policy_digest'],canonical({'identity':before['identity'],'release':before['release']}),now()))
                delta = comparison(store,'1.0.0','1.1.0',before['identity']['policy_digest'])
                evidence_changes = [c for c in delta['changes'] if c.get('historical_mode')=='advisory']
                item = dict(id='policy-example-'+name, project_id=title, title=title, target='local',
                    commit_sha=good['source_revision'], status='UNVERIFIED', original_decision=before['decision'],
                    policy=before['identity'],active=after['identity'],
                    impact=dict(changes=evidence_changes,recheck=True,review=False,redeploy=False,
                                advisory=after['decision']=='BLOCK',reason='evidence_policy_changed'),
                    action='verified' if after['decision']=='PASS' else 'evidence_confirmation_required',
                    reason_codes=[] if after['decision']=='PASS' else [after['reason_code']],
                    latest_recheck={k:after[k] for k in ('decision','reason_code')},
                    policy_data={'results':rows_for_ui(before['results'])+rows_for_ui(after['results'],update=True,offset=len(before['results']))},
                    history={'events':[dict(event,seq=i+1) for i,event in enumerate(
                        before['history']['events']+after['history']['events'])],
                             'diagnostics':before['history']['diagnostics']+after['history']['diagnostics'],
                             'jobs':[]}, read_only=True,record_type='validation_example',
                    service_verified=False,source_input_sha256=fingerprint(value),
                    explanation='고정 소스의 실제 정책 검사 결과입니다. 배포와 서비스 상태는 검증하지 않았습니다.')
                items.append(item)
                summary.append(dict(case=name,before=before['decision'],after=after['decision'],reason=after['reason_code'],
                                    input_sha256=fingerprint(value)))

        missing = copy.deepcopy(items[0])
        missing.update(id='policy-example-missing',project_id='보존 자료 없음',title='snapshot 미보존 · 재검사 불가',
                       action='unavailable',reason_codes=['policy_receipt_unavailable'])
        p = store.create_project('https://github.com/Team-InfraMorph/demo-app','main',['local'])
        did = store.begin_deploy(p['project_id'])
        # A simulated historical LIVE context is confined to this example DB;
        # the actual reevaluator must refuse missing preservation, not invent it.
        with store._lock,store._conn:
            store._conn.execute("UPDATE deployment_targets SET status='LIVE' WHERE deployment_id=?",(did,))
        job = enqueue(store,did,'local')
        run_next(store)
        result = json.loads(store._one('SELECT result FROM policy_jobs WHERE id=?',(job,))[0])
        assert result['decision']=='UNAVAILABLE'
        missing['latest_recheck']=result
        missing['reason_codes']=[result['reason_code']]
        missing['policy_data']={'results':rows_for_ui(items[0]['policy_data']['results'][:6])}
        missing['history']=details(store,did)
        missing['impact'].update(advisory=False,reason='preserved_inputs_unavailable')
        store.set_target_status(did,'local','FAILED')
        store.set_status(did,'FAILED')
        items.append(missing)
        summary.append(dict(case='missing',after=result['decision'],reason=result['reason_code']))
        for name,title,value in [('repair-db','새 배포 · DB 근거 보완',db),
                                 ('repair-worker','새 배포 · worker 근거 보완',worker),
                                 ('repair-exhausted','새 배포 · 같은 근거 반복으로 3회 소진',db)]:
            repaired = await repair_example(store,value,good,name,args.openai)
            rows = repaired['policy_data']['results']
            latest = rows[-1]
            item = copy.deepcopy(items[1 if value is db else 2])
            item.update(id='policy-example-'+name,project_id=title,title=title,
                        original_decision='BLOCK',policy=identity(),active=identity(),
                        policy_data=repaired['policy_data'],history=repaired['history'],
                        latest_recheck={'decision':latest['decision'],'reason_code':latest['reason_code']},
                        action='verified' if latest['decision']=='PASS' else 'action_required',
                        reason_codes=[latest['reason_code']],
                        impact=dict(changes=[],recheck=False,review=False,redeploy=False,advisory=False,reason='new_deployment_repair'),
                        ai_backend=repaired['ai_backend'],stop_reason=repaired['stop_reason'])
            item['description']='분석 근거 수정 검증 · 이후 설계·패치·빌드는 실행하지 않았습니다.'
            item['explanation']=item['description']
            item['inspection_scope']='source_intent'
            items.append(item)
            summary.append(dict(case=name,after=latest['decision'],reason=latest['reason_code'],
                                attempts=len(repaired['policy_data'].get('repairs',[])),backend=repaired['ai_backend'],
                                stop_reason=repaired['stop_reason']))
        value = dict(schema_version=1,generated_at=now(),baseline_commit=BASELINE_COMMIT,
                     baseline_declared_source_commit=baseline['source_commit'],
                     baseline_policy_digest=baseline['identity']['policy_digest'],active=identity(),
                     live_ai='performed' if args.openai else 'not_run',
                     service_verification='not_run',items=items)
        (output/'examples.json').write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
        report = {k:v for k,v in value.items() if k!='items'} | {'cases':summary}
        expectations={'repair-db':('PASS',1),'repair-worker':('PASS',1),'repair-exhausted':('BLOCK',3)}
        valid=all(row['after']==expectations[row['case']][0] and
                  (args.openai or row['attempts']==expectations[row['case']][1])
                  for row in summary if row['case'] in expectations)
        report['validation_status']='passed' if valid else 'failed'
        (output/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(report,ensure_ascii=False))
        return 0 if valid else 1
    finally:
        store.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',required=True,type=Path)
    parser.add_argument('--openai',action='store_true',help='Run DB and worker repair against the existing OpenAI backend')
    parser.add_argument('--env-file',type=Path)
    args=parser.parse_args()
    if args.env_file:
        from dotenv import load_dotenv
        load_dotenv(args.env_file,override=False)
    raise SystemExit(asyncio.run(generate(args)))
