"""Persist policy observations, including failed initial analysis and retries."""
import json
from policy_gate.reporting import observe, result

DDL = '''CREATE TABLE IF NOT EXISTS policy_results (
 seq INTEGER PRIMARY KEY AUTOINCREMENT,
 deployment_id TEXT NOT NULL REFERENCES deployments(id),
 target TEXT NOT NULL, attempt INTEGER NOT NULL, payload TEXT NOT NULL
); CREATE INDEX IF NOT EXISTS policy_results_deployment ON policy_results(deployment_id, seq);'''


def save(store, deployment_id, target, attempt, value):
    if target not in {'local','aws'} or type(attempt) is not int or not 0 <= attempt <= 1:
        raise ValueError('invalid_policy_scope')
    if value.get('decision') not in {'PASS','BLOCK','UNSUPPORTED','ERROR'}:
        raise ValueError('invalid_policy_result')
    with store._lock, store._conn:
        store._conn.execute('INSERT INTO policy_results(deployment_id,target,attempt,payload) VALUES (?,?,?,?)',
                            (deployment_id,target,attempt,json.dumps(value,ensure_ascii=False)))


def read(store, deployment_id):
    with store._lock:
        rows = store._conn.execute('SELECT * FROM policy_results WHERE deployment_id=? ORDER BY seq', (deployment_id,)).fetchall()
    return {'results': [json.loads(r['payload']) | {'seq':r['seq'],'target':r['target'],'attempt':r['attempt']} for r in rows]}


def check(store, deployment_id, target, stage, action, *, attempt=0):
    observations = []
    try:
        with observe(observations.append): output = action()
    except Exception as error:
        if not observations or observations[-1]['decision'] == 'PASS': observations.append(result(stage,error))
        for value in observations: save(store,deployment_id,target,attempt,value)
        raise
    if not observations: observations.append(result(stage))
    for value in observations: save(store,deployment_id,target,attempt,value)
    return output
