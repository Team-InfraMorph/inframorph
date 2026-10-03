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
    payload = json.dumps(value,ensure_ascii=False,sort_keys=True)
    with store._lock, store._conn:
        # The same gate runs again inside the E worker before the build. An observation that
        # repeats byte for byte carries no new fact, so only distinct results are kept.
        if store._conn.execute('SELECT 1 FROM policy_results WHERE deployment_id=? AND target=?'
                               ' AND attempt=? AND payload=? LIMIT 1',
                               (deployment_id,target,attempt,payload)).fetchone():
            return
        store._conn.execute('INSERT INTO policy_results(deployment_id,target,attempt,payload) VALUES (?,?,?,?)',
                            (deployment_id,target,attempt,payload))


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
