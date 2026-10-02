"""Immutable public patch projections; only caller-approved bundles are recorded."""
import hashlib
import json
import os
from pathlib import Path

from analyzer.config import Limits
from analyzer.redaction import Redactor
from analyzer.recovery import Approval, _assert_patch
from analyzer.snapshot import _read_at


DDL = """
CREATE TABLE IF NOT EXISTS deployment_patch_reviews (
    deployment_id TEXT NOT NULL REFERENCES deployments(id),
    target TEXT NOT NULL, phase TEXT NOT NULL,
    fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY (deployment_id, target, phase)
);
"""
MAX_DIFF = 16_000
MAX_PUBLIC = 120_000


def projection(candidate, mapping, approval, phase):
    approval = Approval.model_validate(approval.model_dump())
    if (phase not in {"initial", "recovery"} or not approval.approved or approval.fingerprint != candidate.fingerprint or
            candidate.plan.source_revision != mapping.commit or candidate.manifest["source_revision"] != mapping.commit):
        raise ValueError("patch_review_unapproved")
    _assert_patch(candidate, mapping)
    fd = os.open(candidate.directory, os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW)
    try:
        data = _read_at(fd, "patch.diff", Limits().max_snapshot_bytes)
    finally:
        os.close(fd)
    if hashlib.sha256(data).hexdigest() != candidate.manifest["diff_sha256"]:
        raise ValueError("patch_review_changed")
    sections, current = {}, None
    for line in data.decode().splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            sections[current] = []
        elif current is not None and not line.startswith("--- "):
            sections[current].append(line)
    files = []
    for change in candidate.manifest["changes"]:
        name = change["path"]
        raw = Redactor().clean("\n".join(sections.get(name, []))).encode()
        omitted = Path(name).name in {"package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml"}
        files.append({"path": name, "action": change["action"],
            "before_sha256": change["before_sha256"], "after_sha256": change["after_sha256"],
            "diff": None if omitted else raw[:MAX_DIFF].decode(errors="ignore"),
            "truncated": not omitted and len(raw) > MAX_DIFF})
    result = {"status": candidate.manifest["status"], "files": files, "verified": True,
        "phase": phase, "applied": False, "source_revision": mapping.commit,
        "original_digest": candidate.manifest["original_digest"],
        "patched_digest": candidate.manifest["patched_digest"],
        "diff_sha256": candidate.manifest["diff_sha256"], "fingerprint": candidate.fingerprint}
    if len(json.dumps(result).encode()) > MAX_PUBLIC:
        raise ValueError("patch_review_limit")
    return result


def save(store, deployment_id, candidate, mapping, approval, phase):
    result = projection(candidate, mapping, approval, phase)
    target = candidate.plan.target.value
    encoded = json.dumps(result, sort_keys=True)
    with store._lock, store._conn:
        row = store._conn.execute("SELECT status,commit_sha FROM deployments WHERE id=?", (deployment_id,)).fetchone()
        targets = {r[0] for r in store._conn.execute("SELECT target FROM deployment_targets WHERE deployment_id=?", (deployment_id,))}
        if row is None or row[0] != "DEPLOYING" or row[1] != mapping.commit or target not in targets:
            raise ValueError("patch_review_binding_mismatch")
        previous = store._conn.execute("SELECT fingerprint FROM deployment_patch_reviews WHERE deployment_id=? AND target=? AND phase=?",
                                      (deployment_id, target, phase)).fetchone()
        if previous is not None:
            if previous[0] != result["fingerprint"]:
                raise ValueError("patch_review_already_saved")
            return False
        store._conn.execute("INSERT INTO deployment_patch_reviews VALUES (?, ?, ?, ?, ?)",
                            (deployment_id, target, phase, result["fingerprint"], encoded))
    return True


def applied(store, deployment_id, phase, target="local"):
    with store._lock, store._conn:
        row = store._conn.execute("SELECT payload FROM deployment_patch_reviews WHERE deployment_id=? AND target=? AND phase=?",
                                  (deployment_id, target, phase)).fetchone()
        deployment = store._conn.execute("SELECT status FROM deployments WHERE id=?", (deployment_id,)).fetchone()
        if row is None or deployment is None or deployment[0] != "DEPLOYING":
            raise ValueError("patch_review_not_running")
        value = json.loads(row[0]) | {"applied": True}
        store._conn.execute("UPDATE deployment_patch_reviews SET payload=? WHERE deployment_id=? AND target=? AND phase=?",
                            (json.dumps(value, sort_keys=True), deployment_id, target, phase))


def get(store, deployment_id):
    rows = store._all("SELECT target,phase,payload FROM deployment_patch_reviews WHERE deployment_id=? ORDER BY phase", (deployment_id,))
    values = {}
    for row in rows:
        patch = json.loads(row["payload"])
        if row["target"] in values:
            patch["initial"] = values[row["target"]]
        values[row["target"]] = patch
    return values
