"""Durable observations: commit the decision before allowing the next action."""
import json
from policy_gate.catalog import canonical, identity, rules
from policy_gate.reporting import observe, result, checkpoint

DDL = '''CREATE TABLE IF NOT EXISTS policy_results (
 seq INTEGER PRIMARY KEY AUTOINCREMENT,
 deployment_id TEXT NOT NULL REFERENCES deployments(id),
 target TEXT NOT NULL, attempt INTEGER NOT NULL, payload TEXT NOT NULL
); CREATE INDEX IF NOT EXISTS policy_results_deployment ON policy_results(deployment_id, seq);'''


def start(store,deployment_id,target,value):
    from .policy_lifecycle import audit
    with store._lock,store._conn:
        if not store._conn.execute("SELECT 1 FROM policy_events WHERE execution_id=? AND event='inspection_started'",(value['execution_id'],)).fetchone():
            audit(store,'inspection_started',deployment_id,target,value['execution_id'],occurred_at=value['started_at'],policy_digest=value['policy_digest'],checkpoint=value['checkpoint'])


def save(store, deployment_id, target, attempt, value, *, enforce_binding=True):
    from .policy_lifecycle import guard,audit,diagnostic
    if target not in {'local','aws','gcp','onprem'} or type(attempt) is not int or not 0 <= attempt <= 1:
        raise ValueError('invalid_policy_scope')
    if value.get('decision') not in {'PASS','BLOCK','UNSUPPORTED','ERROR'}:
        raise ValueError('invalid_policy_result')
    managed=value.get('family')=='inframorph-policy'
    if managed:
        if enforce_binding: guard(store,deployment_id)
        if value['policy_digest']!=identity()['policy_digest']:raise ValueError('policy_runtime_changed')
        expected={r['id'] for r in rules(value['stage'])}
        actual=[r['rule_id'] for r in value['rules']]
        if not expected or expected!=set(actual) or len(actual)!=len(set(actual)):raise ValueError('policy_rules_incomplete')
        if value['decision']=='PASS' and (set(value['required_rules'])!={r['rule_id'] for r in value['rules'] if r['required']} or set(value['evaluated_rules'])!={r['rule_id'] for r in value['rules'] if r['decision'] not in {'NOT_RUN','NOT_APPLICABLE'}} or not value['complete'] or not value['rules'] or
                any(r['decision'] not in {'PASS','NOT_APPLICABLE'} for r in value['rules']) or
                set(value['required_rules'])-set(value['evaluated_rules'])):raise ValueError('policy_rules_incomplete')
    payload=canonical(value)
    if len(payload.encode())>256_000:raise ValueError('policy_result_limit')
    with store._lock,store._conn:
        records=store._conn.execute('SELECT payload FROM policy_results WHERE deployment_id=? AND target=? AND attempt=?',(deployment_id,target,attempt)).fetchall()
        for old in records:
            if managed and json.loads(old[0]).get('execution_id')==value['execution_id']:
                if old[0]!=payload:raise ValueError('policy_execution_conflict')
                return
            if not managed and canonical(json.loads(old[0]))==payload:return
        store._conn.execute('INSERT INTO policy_results(deployment_id,target,attempt,payload) VALUES (?,?,?,?)',
                            (deployment_id,target,attempt,payload))
        if managed:
            common=dict(policy_digest=value['policy_digest'],checkpoint=value['checkpoint'])
            start(store,deployment_id,target,value)
            for r in value['rules']:
                audit(store,'rule_evaluated',deployment_id,target,value['execution_id'],occurred_at=r.get('finished_at',value['finished_at']),rule_id=r['rule_id'],decision=r['decision'],reason_code=r['reason_code'])
            audit(store,'inspection_finished',deployment_id,target,value['execution_id'],occurred_at=value['finished_at'],decision=value['decision'],**common)
    if value['decision']!='PASS':
        if not managed:
            with store._lock,store._conn:audit(store,'operational_failure',deployment_id,target,value.get('execution_id'),reason_code=value['reason_code'],decision=value['decision'])
        diagnostic(store,deployment_id,value.get('execution_id'),value['reason_code'])


def read(store, deployment_id):
    rows=store._all('SELECT * FROM policy_results WHERE deployment_id=? ORDER BY seq',(deployment_id,))
    results=[json.loads(r['payload'])|{'seq':r['seq'],'target':r['target'],'attempt':r['attempt']} for r in rows]
    payload={'results':results}
    if any(r.get('family')=='inframorph-policy' for r in results):payload['summaries']=summaries(results)
    return payload


def summaries(results):
    answer=[];required={r['stage'] for r in rules()}
    for target in {r['target'] for r in results}:
        records=[r for r in results if r['target']==target and r.get('family')=='inframorph-policy' and r.get('purpose')!='policy_update']
        if not records:continue
        meta=records[-1];records=[r for r in records if r['policy_digest']==meta['policy_digest']]
        latest={r['stage']:r for r in records}
        decisions={r['decision'] for r in latest.values()}
        complete=required<=latest.keys() and all(r['complete'] for r in latest.values())
        decision=next((x for x in ('ERROR','BLOCK','UNSUPPORTED') if x in decisions),'PASS' if complete else 'NOT_RUN')
        answer.append(dict(target=target,decision=decision,complete=complete,version=meta['version'],policy_digest=meta['policy_digest'],missing_stages=sorted(required-latest.keys())))
    return answer



def check(store, deployment_id, target, stage, action, *, attempt=0):
    from .policy_lifecycle import guard
    guard(store,deployment_id)
    observations=[]
    def sink(value):
        save(store,deployment_id,target,attempt,value)
        observations.append(value)
    try:
        with observe(sink,lambda r:start(store,deployment_id,target,r)),checkpoint(stage): output=action()
    except Exception as error:
        if not observations or observations[-1]['decision']=='PASS':sink(result(stage,error))
        raise
    return output
