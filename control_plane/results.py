"""Validated deployment-specific analysis history and recovery usage accounting."""
import hashlib
import json
import math
import re

from schemas import Intent, Plan, RepoMap


DDL = """
CREATE TABLE IF NOT EXISTS deployment_analysis (
    deployment_id TEXT PRIMARY KEY REFERENCES deployments(id),
    initial_payload TEXT NOT NULL, current_payload TEXT NOT NULL,
    recovery_fingerprint TEXT
);
CREATE TABLE IF NOT EXISTS runtime_runs (
    deployment_id TEXT PRIMARY KEY REFERENCES deployments(id),
    source_revision TEXT NOT NULL, status TEXT NOT NULL
);
"""
COUNTERS = ("model_calls", "api_calls", "tool_calls", "validation_retries",
            "input_tokens", "output_tokens", "duration_ms")


def metrics(value):
    value = value or {}
    if not isinstance(value, dict):
        raise ValueError("invalid_runtime_metrics")
    result = {}
    for key in COUNTERS:
        number = value.get(key, 0)
        if type(number) is not int or not 0 <= number <= 10**12:
            raise ValueError("invalid_runtime_metrics")
        result[key] = number
    cost = value.get("estimated_usd", 0)
    if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
        raise ValueError("invalid_runtime_metrics")
    result["estimated_usd"] = cost
    for key in ("backend", "model", "snapshot_digest"):
        if key in value:
            if key == "snapshot_digest" and value[key] == "":
                continue
            if not isinstance(value[key], str) or not re.fullmatch(r"[a-zA-Z0-9._-]{1,100}", value[key]):
                raise ValueError("invalid_runtime_metrics")
            result[key] = value[key]
    if type(value.get("usage_complete", True)) is not bool:
        raise ValueError("invalid_runtime_metrics")
    result["usage_complete"] = value.get("usage_complete", True)
    return result


def checked_payload(value):
    mapping = RepoMap.model_validate(value["repo_map"])
    intent = Intent.model_validate(value["intent"])
    plans = {target: Plan.model_validate(plan) for target, plan in value["plans"].items()}
    if (intent.source_revision != mapping.commit or not plans or
            any(p.source_revision != mapping.commit or p.target.value != t or p.app != intent.app
                for t, p in plans.items())):
        raise ValueError("runtime_result_binding_mismatch")
    return {"repo_map": mapping.model_dump(mode="json"), "intent": intent.model_dump(mode="json"),
            "plans": {t: p.model_dump(mode="json") for t, p in plans.items()}, "metrics": metrics(value.get("metrics"))}


def put_initial(store, deployment_id, value):
    checked = checked_payload(value)
    encoded = json.dumps(checked, sort_keys=True)
    if len(encoded.encode()) > 512_000:
        raise ValueError("runtime_result_limit")
    with store._lock, store._conn:
        row = store._conn.execute("SELECT commit_sha, status FROM deployments WHERE id=?", (deployment_id,)).fetchone()
        targets = {r[0] for r in store._conn.execute("SELECT target FROM deployment_targets WHERE deployment_id=?", (deployment_id,))}
        if row is None or row[0] != checked["repo_map"]["commit"] or row[1] != "DEPLOYING" or targets != set(checked["plans"]):
            raise ValueError("runtime_result_binding_mismatch")
        previous = store._conn.execute("SELECT initial_payload FROM deployment_analysis WHERE deployment_id=?", (deployment_id,)).fetchone()
        if previous is not None and previous[0] != encoded:
            raise ValueError("initial_analysis_already_saved")
        store._conn.execute("INSERT OR IGNORE INTO deployment_analysis VALUES (?, ?, ?, NULL)",
                            (deployment_id, encoded, encoded))


def get_result(store, deployment_id):
    row = store._one("SELECT initial_payload,current_payload FROM deployment_analysis WHERE deployment_id=?",
                     (deployment_id,))
    if row is None:
        return None
    return {"initial": json.loads(row[0]), **json.loads(row[1])}


def apply_recovery(store, deployment_id, value):
    """Commit corrected artifacts and total usage atomically, at most once."""
    status, reason, attempts = value["status"], value["reason"], value["attempts"]
    if (status not in {"recovered", "failed"} or type(attempts) is not int or attempts not in (0, 1) or
            not isinstance(reason, str) or not re.fullmatch(r"[a-z_]{1,80}", reason)):
        raise ValueError("invalid_recovery_result")
    retry_metrics = metrics(value.get("metrics"))
    # Failed or unapproved model outputs are not promoted into analysis/cache/Plan.
    recovered = checked_payload(value["corrected"]) if status == "recovered" else None
    normalized = {"status": status, "reason": reason, "attempts": attempts,
                  "metrics": retry_metrics, "corrected": recovered}
    signature = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()
    with store._lock, store._conn:
        record = store._conn.execute("SELECT initial_payload,recovery_fingerprint FROM deployment_analysis WHERE deployment_id=?",
                                     (deployment_id,)).fetchone()
        deployment = store._conn.execute("SELECT project_id,commit_sha,status FROM deployments WHERE id=?", (deployment_id,)).fetchone()
        if record is None or deployment is None:
            raise ValueError("runtime_result_missing")
        if record[1] is not None:
            if record[1] != signature:
                raise ValueError("recovery_result_already_applied")
            return False
        if deployment[2] != "DEPLOYING":
            raise ValueError("runtime_result_not_running")
        initial = json.loads(record[0])
        current = recovered or dict(initial)
        if recovered and (recovered["repo_map"] != initial["repo_map"] or set(recovered["plans"]) != set(initial["plans"])):
            raise ValueError("runtime_result_binding_mismatch")
        total = metrics(initial["metrics"])
        for key in (*COUNTERS, "estimated_usd"):
            total[key] += retry_metrics[key]
        total["usage_complete"] &= retry_metrics["usage_complete"]
        if total.get("backend") != retry_metrics.get("backend") and attempts:
            total["backend"] = "mixed"
        recovery = {"status": status, "reason": reason, "attempts": attempts,
                    "initial_metrics": initial["metrics"], "retry_metrics": retry_metrics}
        current = {**current, "metrics": {**total, "recovery": recovery}, "recovery": recovery}
        encoded = json.dumps(current, sort_keys=True)
        if len(encoded.encode()) > 512_000:
            raise ValueError("runtime_result_limit")
        store._conn.execute("UPDATE deployment_analysis SET current_payload=?,recovery_fingerprint=? WHERE deployment_id=?",
                            (encoded, signature, deployment_id))
        store._conn.execute("UPDATE deployments SET analysis_metrics=? WHERE id=?", (json.dumps(current["metrics"]), deployment_id))
        if recovered:
            store._conn.executemany("INSERT OR REPLACE INTO plans VALUES (?, ?, ?)",
                [(deployment_id, t, json.dumps(p)) for t, p in current["plans"].items()])
            # The commit cache serves future Change Detector requests. Historical
            # API reads use deployment_analysis, so older deployments remain intact.
            store._conn.execute("UPDATE analyses SET intent=? WHERE project_id=? AND commit_sha=?",
                (json.dumps(current["intent"]), deployment[0], deployment[1]))
        return True


def claim_run(store, deployment_id, revision):
    with store._lock, store._conn:
        row = store._conn.execute("SELECT commit_sha,status FROM deployments WHERE id=?", (deployment_id,)).fetchone()
        if row is None or row[0] != revision or row[1] != "DEPLOYING":
            raise ValueError("runtime_run_binding_mismatch")
        changed = store._conn.execute("INSERT OR IGNORE INTO runtime_runs VALUES (?, ?, 'running')", (deployment_id, revision)).rowcount
        if not changed:
            raise ValueError("runtime_run_already_started")


def finish_run(store, deployment_id, status):
    if status not in {"succeeded", "failed", "interrupted"}:
        raise ValueError("invalid_runtime_status")
    with store._lock, store._conn:
        store._conn.execute("UPDATE runtime_runs SET status=? WHERE deployment_id=? AND status='running'",
                            (status, deployment_id))
