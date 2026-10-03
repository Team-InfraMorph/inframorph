#!/usr/bin/env python3
"""Exercise Intent/Plan/patch repair with real gates, without Docker or AWS changes."""
import argparse
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analyzer.backend import OpenAIBackend, ReplayBackend, Reply
from analyzer.config import Limits
from analyzer.recovery import PatchedCandidate
from analyzer.redaction import Redactor
from analyzer.snapshot import Snapshot
from analyzer.source_policy import validate_demo_intent, validate_demo_plan
from code_patch.runner import read_snapshot, transform, patch_snapshot, ALLOWED_PATHS
from control_plane.auto_repair import repair, history, usage
from control_plane.db import Store
from control_plane.policy_results import check
from policy_gate.gate import validate_intent, validate_plan, validate_patch
from schemas import Intent, Plan, RepoMap


async def verify(args):
    folder = args.output_dir.absolute()
    if any(p.is_symlink() for p in (folder, *folder.parents)):
        raise ValueError('unsafe_output_directory')
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    source = ROOT/'tests/fixtures/analyzer/v1/snapshot'
    mapping = RepoMap.model_validate_json((ROOT/'tests/fixtures/analyzer/v1/repo_map.json').read_text())
    intent = Intent.model_validate_json((ROOT/'schemas/fixtures/v1/intent.json').read_text())
    plan = Plan.model_validate_json((ROOT/'schemas/fixtures/v1/plan.local.json').read_text())
    limits = Limits(timeout_seconds=180, max_estimated_usd=.1)
    snapshot = Snapshot(source,mapping.tree,limits,Redactor())
    store = Store(folder/'control-plane.db')
    cases = []
    try:
        for stage in ('intent','plan','patch'):
            project = store.create_project('https://github.com/Team-InfraMorph/demo-app','main',['local'])
            did = store.begin_deploy(project['project_id'])
            store.set_commit(did,mapping.commit)
            out = folder/stage
            out.mkdir()
            if stage == 'intent':
                initial = intent.model_copy(update={'unknowns':['deployment secret value is missing']})
                expected = intent.model_dump(mode='json')
                def gate(value, attempt=0):
                    check(store,did,'local','source',lambda:validate_demo_intent(value,source,mapping),attempt=attempt)
                    check(store,did,'local','intent',lambda:validate_intent(value,source,mapping.commit),attempt=attempt)
            elif stage == 'plan':
                initial = plan.model_copy(update={'config':{'STORAGE_DRIVER':'fs','PORT':'3001'}})
                expected = plan.model_dump(mode='json')
                def gate(value, attempt=0):
                    check(store,did,'local','plan',lambda:(validate_demo_plan(value,mapping),validate_plan(intent,value)),attempt=attempt)
            else:
                original,_ = read_snapshot(source,mapping)
                approved = transform(original,plan)
                changed = {n:approved[n] for n in ALLOWED_PATHS if n in approved}
                changed['src/images.js'] += b'\nfunction broken( {\n'
                manifest = patch_snapshot(source,mapping,plan,out/'initial',replacements=changed)
                initial = PatchedCandidate(out/'initial',manifest,plan,tuple(sorted(original.keys()|changed.keys())))
                expected = {'files':[{'path':'src/images.js','content':approved['src/images.js'].decode()}]}
                def gate(value, attempt=0):
                    check(store,did,'local','patch',lambda:validate_patch(source,value.directory,plan),attempt=attempt)
            backend = OpenAIBackend() if args.openai else ReplayBackend([Reply(text=json.dumps(expected))])
            @asynccontextmanager
            async def session():
                yield backend
            try:
                try:
                    gate(initial)
                except ValueError as error:
                    fixed = await repair(store=store,did=did,mapping=mapping,source=source,stage=stage,
                        target='local',initial=initial,error=error,validate=gate,session=session,limits=limits,
                        initial_metrics={'backend':backend.name,'snapshot_digest':snapshot.digest,'usage_complete':True},
                        intent=intent if stage=='plan' else None,output_dir=out)
                else:
                    raise ValueError('fault_not_detected')
                cases.append({'stage':stage,'status':'policy_passed','attempts':len(history(store,did)),
                              'usage':usage(store,did),'deployment_id':did})
            except Exception:
                cases.append({'stage':stage,'status':'failed','attempts':len(history(store,did)),
                              'usage':usage(store,did),'deployment_id':did})
            finally:
                if args.openai:
                    await backend.close()
                # These are gate-only fixtures, not successful deployments.
                store.set_status(did,'FAILED')
        result = {'backend':'openai' if args.openai else 'replay','cases':cases,
                  'scope':'isolated repair and real policy validation; no build or deployment'}
        (folder/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result))
        return 0 if all(c['status']=='policy_passed' for c in cases) else 1
    finally:
        store.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--openai',action='store_true',help='Send reviewed demo fixture code to the team API')
    parser.add_argument('--env-file',type=Path)
    args = parser.parse_args()
    if args.env_file:
        from dotenv import load_dotenv
        load_dotenv(args.env_file,override=False)
    raise SystemExit(asyncio.run(verify(args)))
