"""Policy lifecycle records. Server-owned inputs; observations never bypass gates."""
import json
import re
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from policy_gate.catalog import identity, release, canonical, fingerprint, compare
from policy_gate.reporting import now, observe, checkpoint
from analyzer.redaction import Redactor

DDL = '''
CREATE TABLE IF NOT EXISTS policy_releases(digest TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS policy_active(slot INTEGER PRIMARY KEY CHECK(slot=1), digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS policy_bindings(deployment_id TEXT PRIMARY KEY REFERENCES deployments(id), digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS policy_events(seq INTEGER PRIMARY KEY AUTOINCREMENT, deployment_id TEXT, target TEXT, execution_id TEXT, event TEXT NOT NULL, occurred_at TEXT NOT NULL, observed_at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS policy_events_deployment ON policy_events(deployment_id, seq);
CREATE TABLE IF NOT EXISTS policy_diagnostics(id TEXT PRIMARY KEY, deployment_id TEXT, execution_id TEXT, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, payload TEXT);
CREATE TABLE IF NOT EXISTS policy_receipts(deployment_id TEXT NOT NULL REFERENCES deployments(id), target TEXT NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(deployment_id,target));
CREATE TABLE IF NOT EXISTS policy_jobs(id TEXT PRIMARY KEY, deployment_id TEXT NOT NULL REFERENCES deployments(id), target TEXT NOT NULL, policy_digest TEXT NOT NULL, receipt_digest TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, result TEXT);
CREATE TABLE IF NOT EXISTS policy_reviews(id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES policy_jobs(id), created_at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS policy_failure_reviews(id TEXT PRIMARY KEY, deployment_id TEXT NOT NULL, execution_id TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS policy_impact_assessments(id TEXT PRIMARY KEY, deployment_id TEXT NOT NULL, target TEXT NOT NULL, policy_digest TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS policy_successors(deployment_id TEXT PRIMARY KEY REFERENCES deployments(id), parent_id TEXT NOT NULL, policy_digest TEXT NOT NULL);
'''


def audit(store, event, deployment_id=None, target=None, execution_id=None, occurred_at=None, **payload):
    # Callers supply only owned codes/IDs. Free text goes through diagnostic().
    stamp=now()
    store._conn.execute('INSERT INTO policy_events(deployment_id,target,execution_id,event,occurred_at,observed_at,payload) VALUES (?,?,?,?,?,?,?)',
                        (deployment_id,target,execution_id,event,occurred_at or stamp,stamp,canonical(payload)))


def activate(store):
    meta=identity(); digest=meta['policy_digest']
    with store._lock,store._conn:
        old=store._conn.execute('SELECT digest FROM policy_active WHERE slot=1').fetchone()
        store._conn.execute('INSERT OR IGNORE INTO policy_releases VALUES (?,?,?)',(digest,canonical({'identity':meta,'release':release()}),now()))
        if old is None or old[0]!=digest:
            store._conn.execute('INSERT OR REPLACE INTO policy_active VALUES (1,?)',(digest,))
            audit(store,'policy_activated',previous_digest=old[0] if old else None,policy=meta)
    return meta


def guard(store, deployment_id, *, bind=True):
    meta=identity(); digest=meta['policy_digest']
    with store._lock,store._conn:
        active=store._conn.execute('SELECT digest FROM policy_active WHERE slot=1').fetchone()
        if active is None:
            activate(store)
        elif active[0]!=digest: raise ValueError('policy_runtime_changed')
        successor=store._conn.execute('SELECT policy_digest FROM policy_successors WHERE deployment_id=?',(deployment_id,)).fetchone()
        if successor and successor[0]!=digest:raise ValueError('stale_policy_redeploy')
        previous=store._conn.execute('SELECT digest FROM policy_bindings WHERE deployment_id=?',(deployment_id,)).fetchone()
        if previous and previous[0]!=digest: raise ValueError('policy_changed_recheck_required')
        if bind and previous is None:
            store._conn.execute('INSERT INTO policy_bindings VALUES (?,?)',(deployment_id,digest))
    return meta


def sanitize(text, *, known_secrets=(), limit=8192):
    original=str(text)
    if len(original.encode())>65_536:
        return dict(text='diagnostic_input_limit',masked=True,truncated=True)
    clean=Redactor(known_secrets).clean(original)
    clean=re.sub(r'(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?\S+',r'\1[REDACTED]',clean)
    clean=re.sub(r'(?i)(password|token|api[_-]?key|secret)\s*[:=]\s*[^\s,;]+',r'\1=[REDACTED]',clean)
    clean=''.join(c for c in clean if c in '\n\t' or ord(c)>=32)
    raw=clean.encode()
    return dict(text=raw[:limit].decode(errors='ignore'),masked=clean!=original,truncated=len(raw)>limit)


def diagnostic(store, deployment_id, execution_id, text, *, known_secrets=()):
    """Bound input, redact before truncating; never retain a raw log."""
    ident=str(uuid.uuid4()); stamp=datetime.now(timezone.utc)
    try:
        payload=sanitize(text,known_secrets=known_secrets)
        with store._lock,store._conn:
            store._conn.execute('INSERT INTO policy_diagnostics VALUES (?,?,?,?,?,?)',(ident,deployment_id,execution_id,stamp.isoformat(),(stamp+timedelta(days=30)).isoformat(),canonical(payload)))
    except Exception:
        with store._lock,store._conn: audit(store,'diagnostic_unavailable',deployment_id,execution_id=execution_id)
        return None
    return ident


def expire_logs(store):
    with store._lock,store._conn:
        store._conn.execute('UPDATE policy_diagnostics SET payload=NULL WHERE expires_at<=? AND payload IS NOT NULL',(now(),))


def receipt(store, deployment_id, target, value):
    guard(store,deployment_id)
    if target not in store.get_deployment(deployment_id)['targets']: raise ValueError('invalid_policy_target')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}',value.get('image_id','')): raise ValueError('policy_image_identity_missing')
    digest=fingerprint(value)
    with store._lock,store._conn:
        old=store._conn.execute('SELECT digest FROM policy_receipts WHERE deployment_id=? AND target=?',(deployment_id,target)).fetchone()
        if old and old[0]!=digest: raise ValueError('policy_receipt_conflict')
        store._conn.execute('INSERT OR IGNORE INTO policy_receipts VALUES (?,?,?,?)',(deployment_id,target,digest,canonical(value)))
        audit(store,'deployment_inputs_preserved',deployment_id,target,input_digest=digest,image_id=value['image_id'])


def current_targets(store):
    # Each target's latest successful deployment is independent of sibling failure.
    return store._all('''SELECT d.id,d.project_id,d.commit_sha,d.created_at,t.target,t.status FROM deployments d
        JOIN deployment_targets t ON t.deployment_id=d.id WHERE t.status='LIVE'
        AND NOT EXISTS(SELECT 1 FROM deployments n JOIN deployment_targets nt ON nt.deployment_id=n.id
          WHERE n.project_id=d.project_id AND nt.target=t.target AND nt.status='LIVE'
          AND (n.created_at>d.created_at OR (n.created_at=d.created_at AND n.rowid>d.rowid)))''',())


def comparison(store, base, target, base_policy_digest=None):
    """Version comparison optionally bound to an immutable stored release snapshot."""
    active=identity()
    target_digest = active['policy_digest'] if target == active['version'] else None
    if base_policy_digest is None:
        return compare(base,target) | dict(base_policy_digest=None,target_policy_digest=target_digest)
    unknown = dict(base=base,target=target,changes=[],added=[],removed=[],changed=[],major=False,
                   history_status='confirmation_required',comparison_kind='implementation',
                   base_policy_digest=base_policy_digest,target_policy_digest=target_digest)
    row=store._one('SELECT payload FROM policy_releases WHERE digest=?',(base_policy_digest,))
    if row is None:return unknown
    try:
        stored=json.loads(row[0]);meta=stored['identity'];snapshot=stored['release']
        if (meta['policy_digest']!=base_policy_digest or meta['version']!=base
                or meta['family']!='inframorph-policy' or snapshot['version']!=base):return unknown
        delta=compare(base,target,base_release=snapshot)
        if base_policy_digest==target_digest:
            delta.update(changes=[],added=[],removed=[],changed=[],history_status='known')
        return delta | dict(base_policy_digest=base_policy_digest,target_policy_digest=target_digest)
    except (ValueError,KeyError,TypeError):return unknown


def preserved_scope(store, deployment_id, target):
    """Only verified stored source and claims determine a rule's applicability."""
    from policy_gate.gate import read_tree,digest
    row=store._one('SELECT digest,payload FROM policy_receipts WHERE deployment_id=? AND target=?',(deployment_id,target))
    try:
        if row is None:raise ValueError()
        data=json.loads(row['payload'])
        if fingerprint(data)!=row['digest']:raise ValueError()
        snapshot=Path(data['snapshot'])
        if any(p.is_symlink() for p in (snapshot,*snapshot.parents)):raise ValueError()
        files,_=read_tree(snapshot,filter_source=True)
        if digest(files)!=data['original_digest']:raise ValueError()
        intent=data['intent'];mapping=data['repo_map']
        deployment=store.get_deployment(deployment_id)
        if (intent['source_revision']!=mapping['commit']
                or deployment['commit_sha'] and deployment['commit_sha']!=mapping['commit']):raise ValueError()
        package=json.loads(files.get('package.json',b'{}'))
        if not isinstance(package,dict):raise ValueError()
        scripts=package.get('scripts',{})
        if not isinstance(scripts,dict):raise ValueError()
        return dict(db='prisma/schema.prisma' in files or any(x['kind']=='relational_db' for x in intent['state']),
                    worker=bool(scripts.get('worker')) or 'src/worker.js' in files
                           or any(x['kind']=='worker' for x in intent['workloads']))
    except (ValueError,KeyError,TypeError,OSError):return None


def transition(store, deployment_id, target):
    meta=identity(); binding=store._one('SELECT digest FROM policy_bindings WHERE deployment_id=?',(deployment_id,))
    if target not in {t for change in release()['changes'] for t in change.get('targets',[])}:
        return dict(recheck=True,review=True,redeploy=False,reason='target_scope_requires_review',changes=[])
    if binding and binding[0]==meta['policy_digest']:
        return dict(recheck=False,review=False,redeploy=False,reason='current_policy',changes=[])
    old=store._one('SELECT payload FROM policy_releases WHERE digest=?',(binding[0],)) if binding else None
    base=json.loads(old[0])['identity']['version'] if old else 'legacy'
    try:
        if base==meta['version']:
            delta=comparison(store,base,meta['version'],binding[0] if binding else None)
        else:
            delta=compare(base,meta['version'])
        if delta.get('history_status')=='confirmation_required':raise ValueError('policy_history_unknown')
        candidates=[c for c in delta['changes'] if target in c.get('targets',['local','aws']) and meta['profile'] in c.get('profiles',[meta['profile']])]
        scope=preserved_scope(store,deployment_id,target) if any(c.get('impact_scope') for c in candidates) else None
        rule_impacts=[];changes=[]
        for c in candidates:
            field=c.get('impact_scope')
            applicability=('unknown' if scope is None or field not in scope else 'applicable' if scope[field] else 'not_applicable') if field else 'applicable'
            item=c | dict(applicability=applicability)
            rule_impacts.append(item)
            if applicability!='not_applicable':changes.append(item)
        unknown=any(c['applicability']=='unknown' for c in changes)
        return dict(recheck=bool(changes) and (delta['major'] or any(c.get('recheck') for c in changes)),
                    review=bool(changes) and (delta['major'] or any(c.get('review') for c in changes)),
                    redeploy=any(c.get('redeploy') for c in changes),reason='policy_transition',changes=changes,
                    rule_impacts=rule_impacts,applicability='unknown' if unknown else 'known',
                    advisory=any(c.get('historical_mode')=='advisory' for c in changes),
                    base_policy_digest=binding[0] if binding else None,target_policy_digest=meta['policy_digest'],
                    comparison_kind=delta.get('comparison_kind','version'))
    except ValueError:
        return dict(recheck=True,review=True,redeploy=False,reason='history_requires_review',changes=[],
                    applicability='unknown',history_status='confirmation_required')


def enqueue(store, deployment_id, target, *, automatic=False):
    deployment=store.get_deployment(deployment_id)
    if not deployment or target not in deployment['targets']: raise ValueError('invalid_policy_target')
    if deployment['targets'][target]['status']!='LIVE': raise ValueError('policy_requires_live_target')
    meta=identity(); row=store._one('SELECT digest FROM policy_receipts WHERE deployment_id=? AND target=?',(deployment_id,target))
    digest=row[0] if row else 'unavailable'
    with store._lock,store._conn:
        found=store._conn.execute('SELECT id,status FROM policy_jobs WHERE deployment_id=? AND target=? AND policy_digest=? AND receipt_digest=? ORDER BY rowid DESC LIMIT 1',
                                (deployment_id,target,meta['policy_digest'],digest)).fetchone()
        if found and (automatic or found['status'] in {'queued','running'}): return found['id']
        ident=str(uuid.uuid4());stamp=now()
        store._conn.execute('INSERT INTO policy_jobs VALUES (?,?,?,?,?,?,?,?,NULL)',(ident,deployment_id,target,meta['policy_digest'],digest,'queued',stamp,stamp))
        audit(store,'recheck_requested',deployment_id,target,ident,policy_digest=meta['policy_digest'],automatic=automatic)
    return ident


def schedule(store):
    expire_logs(store)
    for row in current_targets(store):
        if assess(store,row['id'],row['target'])['recheck']: enqueue(store,row['id'],row['target'],automatic=True)


def run_next(store):
    from .policy_results import save,start
    from schemas import Intent, Plan, RepoMap, BuildArtifact
    from analyzer.source_policy import validate_demo_intent,validate_demo_plan
    from policy_gate.gate import validate_intent,validate_plan,validate_patch,digest,read_tree
    from builder.runtime import check_build_profile
    with store._lock,store._conn:
        job=store._conn.execute("SELECT * FROM policy_jobs WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
        if job is None:return False
        if store._conn.execute("UPDATE policy_jobs SET status='running',updated_at=? WHERE id=? AND status='queued'",(now(),job['id'])).rowcount!=1:return True
    reports=[];outcome='UNAVAILABLE';code='recheck_inputs_unavailable'
    try:
        if job['policy_digest']!=identity()['policy_digest']:raise ValueError('policy_changed')
        row=store._one('SELECT * FROM policy_receipts WHERE deployment_id=? AND target=?',(job['deployment_id'],job['target']))
        if row is None or row['digest']!=job['receipt_digest']:raise ValueError('receipt_unavailable')
        data=json.loads(row['payload'])
        if fingerprint(data)!=row['digest']:raise ValueError('receipt_changed')
        mapping=RepoMap.model_validate(data['repo_map']);intent=Intent.model_validate(data['intent']);plan=Plan.model_validate(data['plan'])
        artifact=BuildArtifact.model_validate(data['artifact'])
        if artifact.source_revision!=mapping.commit or artifact.target.value!=job['target'] or plan.target.value!=job['target']:raise ValueError('artifact_changed')
        deployment=store.get_deployment(job['deployment_id'])
        if deployment['commit_sha'] and deployment['commit_sha']!=mapping.commit:raise ValueError('source_revision_changed')
        snapshot=Path(data['snapshot']);bundle=Path(data['bundle'])
        if any(p.is_symlink() for path in (snapshot,bundle) for p in (path,*path.parents)):raise ValueError('inputs_changed')
        if digest(read_tree(snapshot,filter_source=True)[0])!=data['original_digest']:raise ValueError('inputs_changed')
        # Only preserved, verified deployment identity is attested; no claim about live drift.
        if not re.fullmatch(r'sha256:[0-9a-f]{64}',data['image_id']):raise ValueError('image_unavailable')
        def sink(report):
            report['parent_execution_id']=job['id']; report['purpose']='policy_update'
            save(store,job['deployment_id'],job['target'],0,report,enforce_binding=False)
            reports.append(report)
        with observe(sink,lambda r:start(store,job['deployment_id'],job['target'],r)),checkpoint('policy_update'):
            validate_demo_intent(intent,snapshot,mapping)
            validate_intent(intent,snapshot,mapping.commit)
            validate_demo_plan(plan,mapping,target=job['target'])
            validate_plan(intent,plan)
            verified=validate_patch(snapshot,bundle,plan)
            if verified.patched_digest!=data['patched_digest'] or verified.diff_sha256!=data['diff_sha256']:raise ValueError('inputs_changed')
            check_build_profile(verified.files)
        outcome,code='PASS','passed'
    except Exception:
        if reports and reports[-1]['decision']!='PASS':outcome,code=reports[-1]['decision'],reports[-1]['reason_code']
    payload=dict(decision=outcome,reason_code=code,executions=[r['execution_id'] for r in reports],live_drift_checked=False)
    with store._lock,store._conn:
        store._conn.execute("UPDATE policy_jobs SET status='finished',updated_at=?,result=? WHERE id=?",(now(),canonical(payload),job['id']))
        audit(store,'recheck_finished',job['deployment_id'],job['target'],job['id'],**payload)
    return True


def review(store, deployment_id, job_id, expected_digest):
    with store._lock,store._conn:
        job=store._conn.execute('SELECT * FROM policy_jobs WHERE id=? AND deployment_id=?',(job_id,deployment_id)).fetchone()
        receipt_row=store._conn.execute('SELECT digest FROM policy_receipts WHERE deployment_id=? AND target=?',(deployment_id,job['target'] if job else '')).fetchone()
        if (job is None or job['status']!='finished' or json.loads(job['result'])['decision']!='PASS'
            or job['policy_digest']!=expected_digest or expected_digest!=identity()['policy_digest']
            or not receipt_row or receipt_row[0]!=job['receipt_digest']):raise ValueError('stale_or_failed_policy_review')
        receipt_data=store._conn.execute('SELECT payload FROM policy_receipts WHERE deployment_id=? AND target=?',(deployment_id,job['target'])).fetchone()
        verify_preserved(json.loads(receipt_data[0]))
        value=dict(policy_digest=expected_digest,receipt_digest=job['receipt_digest'],actor='local_operator',identity_verified=False)
        ident=str(uuid.uuid4())
        store._conn.execute('INSERT INTO policy_reviews VALUES (?,?,?,?)',(ident,job_id,now(),canonical(value)))
        audit(store,'policy_reviewed',deployment_id,job['target'],job_id,**value)
    return ident


def failure_review(store,deployment_id,execution_id,value):
    allowed={'violation','false_positive','unsupported','validator_error','environment_error','unknown'}
    if value.get('classification') not in allowed:raise ValueError('invalid_failure_classification')
    rows=store._all('SELECT seq,target,payload FROM policy_results WHERE deployment_id=?',(deployment_id,))
    original=next((r for r in rows if json.loads(r['payload']).get('execution_id')==execution_id),None)
    record=json.loads(original['payload']) if original else None
    if not record or record['decision']=='PASS':raise ValueError('failure_record_missing')
    commit=value.get('fix_commit','');test=value.get('regression_test','');resolved=value.get('resolved_execution_id','')
    if commit and not re.fullmatch('[0-9a-f]{40}',commit):raise ValueError('invalid_fix_commit')
    if test and (not re.fullmatch('[A-Za-z0-9_./:-]{1,200}',test) or '..' in test):raise ValueError('invalid_regression_test')
    resolution={}
    if resolved:
        related=store._all('SELECT r.payload,r.target,r.seq,r.deployment_id,d.commit_sha FROM policy_results r JOIN deployments d ON d.id=r.deployment_id WHERE d.project_id=(SELECT project_id FROM deployments WHERE id=?)',(deployment_id,))
        matched=next((r for r in related if json.loads(r['payload']).get('execution_id')==resolved),None)
        candidate=json.loads(matched['payload']) if matched else None
        failed_rules={r['rule_id']:r.get('revision',0) for r in record.get('rules',[]) if r['decision'] in {'BLOCK','ERROR','UNSUPPORTED'}}
        passed_rules={r['rule_id']:r.get('revision',0) for r in (candidate or {}).get('rules',[]) if r['decision']=='PASS'}
        if (not candidate or candidate['decision']!='PASS' or not candidate.get('complete')
                or candidate['stage']!=record['stage'] or matched['target']!=original['target']
                or matched['seq']<=original['seq'] or not failed_rules
                or candidate.get('family')!=record.get('family')
                or any(passed_rules.get(k,-1)<revision for k,revision in failed_rules.items())):
            raise ValueError('resolved_execution_not_passed')
        resolution=dict(resolved_deployment_id=matched['deployment_id'],resolved_target=matched['target'],
                        resolved_policy_digest=candidate.get('policy_digest'),
                        resolved_source_revision=matched['commit_sha'] or candidate.get('binding',{}).get('source_revision'))
    summary=sanitize(value.get('summary',''),limit=2000)['text']
    if value['classification']=='false_positive' and (not summary or not test):raise ValueError('false_positive_requires_evidence')
    payload=dict(classification=value['classification'],summary=summary,fix_commit=commit,regression_test=test,resolved_execution_id=resolved,actor='local_operator',identity_verified=False,**resolution)
    with store._lock,store._conn:
        ident=str(uuid.uuid4());store._conn.execute('INSERT INTO policy_failure_reviews VALUES (?,?,?,?,?)',(ident,deployment_id,execution_id,now(),canonical(payload)))
        audit(store,'failure_reviewed',deployment_id,execution_id=execution_id,review_id=ident,classification=value['classification'])
    return ident


def details(store,deployment_id):
    expire_logs(store)
    jobs=[dict(r) for r in store._all('SELECT * FROM policy_jobs WHERE deployment_id=? ORDER BY rowid',(deployment_id,))]
    for j in jobs:
        j['result']=json.loads(j['result']) if j['result'] else None
        j['reviewed']=bool(store._one('SELECT 1 FROM policy_reviews WHERE job_id=?',(j['id'],)))
    events=[dict(r)|{'payload':json.loads(r['payload'])} for r in store._all('SELECT * FROM policy_events WHERE deployment_id=? ORDER BY seq',(deployment_id,))]
    logs=[dict(r)|{'payload':json.loads(r['payload']) if r['payload'] and r['expires_at']>now() else None} for r in store._all('SELECT * FROM policy_diagnostics WHERE deployment_id=?',(deployment_id,))]
    failures=[dict(r)|{'payload':json.loads(r['payload'])} for r in store._all('SELECT * FROM policy_failure_reviews WHERE deployment_id=? ORDER BY rowid',(deployment_id,))]
    assessments=[dict(r)|{'payload':json.loads(r['payload'])} for r in store._all('SELECT * FROM policy_impact_assessments WHERE deployment_id=? ORDER BY rowid',(deployment_id,))]
    return dict(jobs=jobs,events=events,diagnostics=logs,failure_reviews=failures,impact_assessments=assessments)


def original_decision(records, stored_release):
    """Summarize the original ledger against its own catalog, never infer from LIVE."""
    if not stored_release or not stored_release.get('rules'):return None
    required={r['stage'] for r in stored_release['rules']}
    managed=[r for r in records if r.get('family')=='inframorph-policy']
    if not managed:return None
    digest=managed[-1].get('policy_digest')
    latest={r['stage']:r for r in managed if r.get('policy_digest')==digest}
    decisions={r['decision'] for r in latest.values()}
    for failure in ('ERROR','BLOCK','UNSUPPORTED'):
        if failure in decisions:return failure
    return 'PASS' if required<=latest.keys() and all(r.get('complete') for r in latest.values()) else 'NOT_RUN'


def impacts(store):
    active=identity();items=[]
    for row in current_targets(store):
        item=dict(row);did=row['id'];target=row['target'];binding=store._one('SELECT digest FROM policy_bindings WHERE deployment_id=?',(did,))
        old=store._one('SELECT payload FROM policy_releases WHERE digest=?',(binding[0],)) if binding else None
        original=[json.loads(r[0]) for r in store._all('SELECT payload FROM policy_results WHERE deployment_id=? AND target=? ORDER BY seq',(did,target))]
        original=[r for r in original if r.get('purpose')!='policy_update' and r.get('family')!='diagnostic']
        historical=original[-1] if original else None
        meta=json.loads(old[0])['identity'] if old else dict(family=historical.get('family','legacy') if historical else 'unknown',version=historical.get('version','기록 없음') if historical else '기록 없음')

        change=transition(store,did,target)
        job=store._one('SELECT * FROM policy_jobs WHERE deployment_id=? AND target=? AND policy_digest=? ORDER BY rowid DESC LIMIT 1',(did,target,active['policy_digest']))
        status='current' if not change['recheck'] else 'evidence_recheck_recommended' if change.get('advisory') else 'recheck_required'
        if status=='current' and change['reason']=='current_policy':
            from .policy_results import read
            summary=next((r for r in read(store,did).get('summaries',[]) if r['target']==target),None)
            if not summary or summary['decision']!='PASS':status='inspection_incomplete'
        if job:
            status=job['status']
            if job['result']:
                decision=json.loads(job['result'])['decision']
                reviewed=bool(store._one('SELECT 1 FROM policy_reviews WHERE job_id=?',(job['id'],)))
                status=('review_required' if change['review'] and not reviewed else 'redeploy_required' if change['redeploy'] else 'verified') if decision=='PASS' else 'unavailable' if decision=='UNAVAILABLE' else 'action_required'
                if (decision=='BLOCK' and change.get('advisory') and json.loads(job['result'])['reason_code'] in
                        {'db_provider_evidence_missing','worker_command_evidence_missing','worker_start_evidence_missing',
                         'db_evidence_unrelated','worker_evidence_unrelated'}):
                    status='evidence_confirmation_required'
        codes={r.get('reason_code') for r in original if r.get('decision')!='PASS' and r.get('reason_code')}
        if job and job['result'] and json.loads(job['result'])['decision']!='PASS':codes.add(json.loads(job['result'])['reason_code'])
        item.update(reason_codes=sorted(codes),policy=meta,active=active,impact=change,action=status,job_id=job['id'] if job else None,
                    original_decision=original_decision(original,json.loads(old[0]).get('release') if old else None),
                    latest_recheck=json.loads(job['result']) if job and job['result'] else None,
                    record_type='deployment',read_only=False,service_unchanged=True)
        items.append(item)
    return items


def statistics(store):
    """Counts of observed rule executions, never an attack-detection probability."""
    grouped={}
    reviews={r['execution_id']:json.loads(r['payload']) for r in store._all('SELECT * FROM policy_failure_reviews ORDER BY rowid',())}
    for row in store._all('SELECT payload FROM policy_results ORDER BY seq',()):
        result=json.loads(row['payload']);review=reviews.get(result.get('execution_id'),{})
        for rule in result.get('rules',[]):
            key=(result['family'],result['version'],rule['rule_id'],result.get('purpose','deployment'))
            item=grouped.setdefault(key,dict(family=key[0],version=key[1],rule_id=key[2],purpose=key[3],performed=0,passed=0,blocked=0,errors=0,unsupported=0,not_run=0,not_applicable=0,false_positive=0,unresolved=0,reasons={}))
            decision=rule['decision'];name={'PASS':'passed','BLOCK':'blocked','ERROR':'errors','UNSUPPORTED':'unsupported','NOT_RUN':'not_run','NOT_APPLICABLE':'not_applicable'}[decision]
            item[name]+=1
            if decision not in {'NOT_RUN','NOT_APPLICABLE'}:item['performed']+=1
            if decision in {'BLOCK','ERROR','UNSUPPORTED'}:
                item['reasons'][rule['reason_code']]=item['reasons'].get(rule['reason_code'],0)+1
                item['false_positive']+=int(review.get('classification')=='false_positive')
                item['unresolved']+=int(not review.get('resolved_execution_id'))
    return list(grouped.values())


def verify_preserved(data):
    from policy_gate.gate import read_tree,digest,sha
    snapshot=Path(data['snapshot']);bundle=Path(data['bundle'])
    try:
        if any(p.is_symlink() for path in (snapshot,bundle) for p in (path,*path.parents)):raise ValueError()
        original=read_tree(snapshot,filter_source=True)[0];files=read_tree(bundle)[0]
        patched={name[7:]:value for name,value in files.items() if name.startswith('source/')}
        if digest(original)!=data['original_digest'] or digest(patched)!=data['patched_digest'] or sha(files['patch.diff'])!=data['diff_sha256']:raise ValueError()
    except Exception:raise ValueError('preserved_inputs_changed') from None


def assess(store,deployment_id,target):
    change=transition(store,deployment_id,target);meta=identity()
    binding=store._one('SELECT digest FROM policy_bindings WHERE deployment_id=?',(deployment_id,))
    receipt=store._one('SELECT digest FROM policy_receipts WHERE deployment_id=? AND target=?',(deployment_id,target))
    key=fingerprint(dict(deployment=deployment_id,target=target,policy=meta['policy_digest'],previous=binding[0] if binding else None,input=receipt[0] if receipt else None,change=change))
    with store._lock,store._conn:
        inserted=store._conn.execute('INSERT OR IGNORE INTO policy_impact_assessments VALUES (?,?,?,?,?,?)',(key,deployment_id,target,meta['policy_digest'],now(),canonical(change))).rowcount
        if inserted:audit(store,'policy_impact_assessed',deployment_id,target,assessment_id=key,policy_digest=meta['policy_digest'],recheck=change['recheck'],review=change['review'],redeploy=change['redeploy'],reason=change['reason'])
    return change
