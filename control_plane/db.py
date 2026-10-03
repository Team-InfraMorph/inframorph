"""SQLite 저장소. 화면은 항상 DB에서 읽으므로 새로고침·재시작 후에도 상태가 유지된다."""
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from schemas.common import check_revision


class Status(str, Enum):
    CREATED = "CREATED"
    ANALYZED = "ANALYZED"
    PLANNED = "PLANNED"
    PATCHED = "PATCHED"
    DEPLOYING = "DEPLOYING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    LIVE = "LIVE"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"
    SUPERSEDED = "SUPERSEDED"  # 시작 전에 더 새 커밋의 요청이 와서 건너뛴 작업


TERMINAL = {Status.LIVE, Status.FAILED, Status.ROLLED_BACK, Status.SUPERSEDED}
BLOCKING = (Status.DEPLOYING.value, Status.AWAITING_APPROVAL.value)  # 이 상태가 있으면 새 배포는 대기

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    repo_url TEXT NOT NULL,
    repo_key TEXT NOT NULL,
    branch TEXT NOT NULL,
    targets TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deployments (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    status TEXT NOT NULL,
    commit_sha TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    triggered_by TEXT NOT NULL DEFAULT 'manual',
    analysis_mode TEXT,
    change_reasons TEXT
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    deployment_id TEXT NOT NULL REFERENCES deployments(id),
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analyses (
    project_id TEXT NOT NULL REFERENCES projects(id),
    commit_sha TEXT NOT NULL,
    repo_map TEXT NOT NULL,
    intent TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, commit_sha)
);
CREATE TABLE IF NOT EXISTS deployment_targets (
    deployment_id TEXT NOT NULL REFERENCES deployments(id),
    target TEXT NOT NULL,
    status TEXT NOT NULL,
    url TEXT,
    PRIMARY KEY (deployment_id, target)
);
CREATE TABLE IF NOT EXISTS plans (
    deployment_id TEXT NOT NULL REFERENCES deployments(id),
    target TEXT NOT NULL,
    plan TEXT NOT NULL,
    PRIMARY KEY (deployment_id, target)
);
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    delivery_id TEXT PRIMARY KEY,
    received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_deployment ON events(deployment_id, seq);
CREATE INDEX IF NOT EXISTS idx_deployments_project ON deployments(project_id, created_at);
"""


# 예전 DB 파일에는 없을 수 있는 열. 시작할 때 없으면 추가한다(데이터는 그대로 둔다).
DEPLOYMENT_COLUMNS = {
    "triggered_by": "TEXT NOT NULL DEFAULT 'manual'",
    "analysis_mode": "TEXT",
    "change_reasons": "TEXT",
    "approval_reasons": "TEXT",
    "approved_at": "TEXT",
    "rollback_of": "TEXT",
    "analysis_metrics": "TEXT",
}
TARGET_COLUMNS = {"verification": "TEXT"}  # 조종실이 직접 호출해 본 결과(JSON)
INSERT_DEPLOYMENT = (
    "INSERT INTO deployments (id, project_id, status, commit_sha, created_at, updated_at, triggered_by, "
    "analysis_mode, change_reasons) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


class ConflictError(Exception):
    """같은 프로젝트에 진행 중인 배포가 이미 있다."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 연결 하나를 API 스레드·배포기 스레드가 같이 쓰므로 읽기도 쓰기와 같은 잠금 안에서만 한다.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            from .policy_results import DDL as POLICY_DDL
            self._conn.executescript(POLICY_DDL)
            from .policy_lifecycle import DDL as LIFECYCLE_DDL
            self._conn.executescript(LIFECYCLE_DDL)
            from .results import DDL
            self._conn.executescript(DDL)
            from .patch_reviews import DDL as PATCH_DDL
            self._conn.executescript(PATCH_DDL)
            existing = {row[1] for row in self._conn.execute("PRAGMA table_info(deployments)")}
            for name, ddl in DEPLOYMENT_COLUMNS.items():
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE deployments ADD COLUMN {name} {ddl}")
            existing = {row[1] for row in self._conn.execute("PRAGMA table_info(deployment_targets)")}
            for name, ddl in TARGET_COLUMNS.items():
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE deployment_targets ADD COLUMN {name} {ddl}")

    def _one(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _all(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def close(self):
        with self._lock:
            self._conn.close()

    def save_validated_patch(self, deployment_id, candidate, mapping, approval, phase):
        from .patch_reviews import save
        return save(self, deployment_id, candidate, mapping, approval, phase)

    def mark_patch_applied(self, deployment_id, phase, target="local"):
        from .patch_reviews import applied
        return applied(self, deployment_id, phase, target)

    def get_runtime_patches(self, deployment_id):
        from .patch_reviews import get
        return get(self, deployment_id)

    def recover_interrupted(self):
        """서버가 꺼질 때 진행 중이던 배포는 결과를 알 수 없으므로 FAILED로 둔다."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE deployment_targets SET status=? WHERE status=?", (Status.FAILED.value, Status.DEPLOYING.value)
            )
            cur = self._conn.execute(
                "UPDATE deployments SET status=?, updated_at=? WHERE status IN (?)",
                (Status.FAILED.value, _now(), Status.DEPLOYING.value),
            )
            return cur.rowcount

    def create_project(self, repo_url, branch, targets, repo_key=None):
        project_id, deployment_id, now = _new_id("p"), _new_id("d"), _now()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO projects (id, repo_url, repo_key, branch, targets, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (project_id, repo_url, repo_key or repo_url, branch, json.dumps(targets), now),
            )
            self._conn.execute(
                INSERT_DEPLOYMENT,
                (deployment_id, project_id, Status.CREATED.value, None, now, now, "manual", None, None),
            )
        return {"project_id": project_id, "deployment_id": deployment_id, "status": Status.CREATED.value}

    def get_project(self, project_id):
        row = self._one("SELECT * FROM projects WHERE id=?", (project_id,))
        if row is None:
            return None
        latest = self._one(
            "SELECT id FROM deployments WHERE project_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (project_id,),
        )
        return {
            "project_id": row["id"],
            "repo_url": row["repo_url"],
            "branch": row["branch"],
            "targets": json.loads(row["targets"]),
            "created_at": row["created_at"],
            "latest_deployment_id": latest["id"] if latest else None,
        }

    def list_projects(self):
        rows = self._all("SELECT id FROM projects ORDER BY created_at DESC, rowid DESC")
        return [self.get_project(r["id"]) for r in rows]

    def find_projects(self, repo_key, branch):
        rows = self._all(
            "SELECT id FROM projects WHERE repo_key=? AND branch=? ORDER BY created_at", (repo_key, branch)
        )
        return [r["id"] for r in rows]

    def _deployment(self, row):
        data = dict(row)
        for key in ("change_reasons", "approval_reasons"):
            data[key] = json.loads(data[key]) if data.get(key) else []
        data["analysis_metrics"] = json.loads(data["analysis_metrics"]) if data.get("analysis_metrics") else None
        targets = self._all(
            "SELECT target, status, url, verification FROM deployment_targets WHERE deployment_id=? ORDER BY target",
            (row["id"],),
        )
        data["targets"] = {t["target"]: {"status": t["status"], "url": t["url"],
                                         "verification": json.loads(t["verification"]) if t["verification"] else None}
                           for t in targets}
        return data

    def get_deployment(self, deployment_id):
        row = self._one("SELECT * FROM deployments WHERE id=?", (deployment_id,))
        return self._deployment(row) if row else None

    def list_deployments(self, project_id):
        rows = self._all(
            "SELECT * FROM deployments WHERE project_id=? ORDER BY created_at DESC, rowid DESC", (project_id,)
        )
        return [self._deployment(r) for r in rows]

    def create_push_deployment(self, project_id, commit_sha, mode, reasons):
        """push로 생긴 배포 요청을 CREATED로 기록한다. 실행은 begin_deploy가 맡는다."""
        deployment_id, now = _new_id("d"), _now()
        with self._lock, self._conn:
            self._conn.execute(
                INSERT_DEPLOYMENT,
                (deployment_id, project_id, Status.CREATED.value, commit_sha, now, now, "push", mode,
                 json.dumps(list(reasons), ensure_ascii=False)),
            )
        return deployment_id

    def save_analysis(self, project_id, commit_sha, repo_map, intent=None):
        """B의 repo_map과 C의 intent를 커밋 단위로 저장한다. 스키마와 커밋이 맞지 않으면 ValueError."""
        from pydantic import ValidationError

        from schemas.intent import Intent
        from schemas.repo_map import RepoMap

        try:
            checked_map = RepoMap.model_validate(repo_map)
            checked_intent = Intent.model_validate(intent) if intent is not None else None
        except ValidationError as exc:
            raise ValueError(f"invalid analysis artifact: {exc.errors()[0]['msg']}") from exc
        if checked_map.commit != commit_sha:
            raise ValueError("repo_map.commit does not match commit_sha")
        if checked_intent is not None and checked_intent.source_revision != commit_sha:
            raise ValueError("intent.source_revision does not match commit_sha")
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO analyses VALUES (?, ?, ?, ?, ?)",
                (project_id, commit_sha, checked_map.model_dump_json(),
                 checked_intent.model_dump_json() if checked_intent else None, _now()),
            )

    def get_analysis(self, project_id, commit_sha):
        row = self._one(
            "SELECT repo_map, intent FROM analyses WHERE project_id=? AND commit_sha=?", (project_id, commit_sha)
        )
        if row is None:
            return None
        return {"repo_map": json.loads(row["repo_map"]),
                "intent": json.loads(row["intent"]) if row["intent"] else None}

    def has_queued(self, project_id):
        latest = self._one(
            "SELECT status FROM deployments WHERE project_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (project_id,),
        )
        return latest is not None and latest["status"] == Status.CREATED.value

    def begin_deploy(self, project_id, *, revision=None, targets=None):
        """진행 중 배포가 없을 때만 DEPLOYING으로 바꾼다. 확인과 변경을 한 잠금 안에서 해서 동시 요청을 막는다.

        가장 최근 CREATED 작업을 실행하고, 그보다 오래된 CREATED 작업은 SUPERSEDED로 닫는다.
        대상(local·aws)마다 상태 행을 만든다.
        명시한 revision은 수동 요청으로 고정하며 대기 중인 push의 커밋을 덮어쓰지 않는다.
        targets를 주면 이번 배포는 그 대상에만 나가고, 프로젝트의 기본 대상(push 재배포용)도 그것으로 바꾼다.
        """
        if revision is not None:
            check_revision(revision)
        if targets is not None:
            targets = [t for t in ("local", "onprem", "aws") if t in set(targets)]
            if not targets:
                raise ValueError("deploy_targets_required")
        with self._lock, self._conn:
            if self._one(
                "SELECT 1 FROM deployments WHERE project_id=? AND status IN (?, ?)", (project_id, *BLOCKING)
            ):
                raise ConflictError(project_id)
            latest = self._one(
                "SELECT id, status, triggered_by, commit_sha FROM deployments WHERE project_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (project_id,),
            )
            now = _now()
            mode = "full_analysis" if revision is not None else None
            reasons = json.dumps(["선택한 데모 버전의 전체 분석"]) if revision is not None else None
            if latest and latest["status"] == Status.CREATED.value and (revision is None or
                    (latest["triggered_by"] == "manual" and latest["commit_sha"] is None)):
                deployment_id = latest["id"]
                if revision is not None:
                    self._conn.execute("UPDATE deployments SET commit_sha=?, analysis_mode=?, change_reasons=? WHERE id=?",
                                       (revision, mode, reasons, deployment_id))
            else:
                deployment_id = _new_id("d")
                self._conn.execute(
                    INSERT_DEPLOYMENT,
                    (deployment_id, project_id, Status.CREATED.value, revision, now, now, "manual", mode, reasons),
                )
            self._conn.execute(
                "UPDATE deployments SET status=?, updated_at=? WHERE project_id=? AND status=? AND id<>?",
                (Status.SUPERSEDED.value, now, project_id, Status.CREATED.value, deployment_id),
            )
            self._conn.execute(
                "UPDATE deployments SET status=?, updated_at=? WHERE id=?",
                (Status.DEPLOYING.value, now, deployment_id),
            )
            if self._one("SELECT triggered_by FROM deployments WHERE id=?", (deployment_id,))["triggered_by"] == "rollback":
                # Queued rollbacks retain their reviewed scope even if the
                # project's default destinations changed while they waited.
                targets = list(self.get_plans(deployment_id))
                if not targets:
                    raise ValueError("rollback_target_mismatch")
            if targets is None:
                targets = json.loads(self._one(
                    "SELECT targets FROM projects WHERE id=?", (project_id,))["targets"])
            else:
                self._conn.execute("UPDATE projects SET targets=? WHERE id=?", (json.dumps(targets), project_id))
            self._conn.executemany(
                "INSERT OR REPLACE INTO deployment_targets (deployment_id, target, status, url) VALUES (?, ?, ?, NULL)",
                [(deployment_id, target, Status.DEPLOYING.value) for target in targets],
            )
        return deployment_id

    def retry_validated_deployment(self, deployment_id):
        """New manual run of the same reviewed revision, using the validated cache.

        Keep the failed history intact; never reuse rejected Analyzer output.
        The runtime rechecks the source digest, Intent, Plan, patch and image.
        """
        with self._lock, self._conn:
            previous = self.get_deployment(deployment_id)
            analysis = self.get_deployment_analysis(deployment_id)
            if (previous is None or previous["status"] != Status.FAILED.value or analysis is None or
                    previous["commit_sha"] is None or self.get_analysis(previous["project_id"], previous["commit_sha"]) is None):
                raise ValueError("validated_retry_unavailable")
            if self._one("SELECT 1 FROM deployments WHERE project_id=? AND status IN (?, ?)",
                         (previous["project_id"], *BLOCKING)):
                raise ConflictError(previous["project_id"])
            new_id, now = _new_id("d"), _now()
            self._conn.execute(INSERT_DEPLOYMENT, (new_id, previous["project_id"], Status.CREATED.value,
                previous["commit_sha"], now, now, "manual", "rebuild_only", json.dumps(["검증된 동일 커밋의 분석을 재사용해 다시 배포"])))
            return self.begin_deploy(previous["project_id"])

    def set_target_status(self, deployment_id, target, status, url=None):
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE deployment_targets SET status=?, url=COALESCE(?, url) WHERE deployment_id=? AND target=?",
                (Status(status).value, url, deployment_id, target),
            )

    def set_verification(self, deployment_id, target, result):
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE deployment_targets SET verification=? WHERE deployment_id=? AND target=?",
                (json.dumps(result, ensure_ascii=False), deployment_id, target),
            )

    def set_commit(self, deployment_id, commit_sha):
        """수동 배포는 스냅샷 단계에서 커밋이 정해진다. 이미 정해진 커밋은 바꾸지 않는다."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE deployments SET commit_sha=COALESCE(commit_sha, ?) WHERE id=?", (commit_sha, deployment_id)
            )

    def set_analysis_metrics(self, deployment_id, metrics):
        """Analyzer가 보고한 모델 호출 수·토큰·예상 비용. 화면의 'AI 호출 수·비용' 표시에 쓴다."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE deployments SET analysis_metrics=? WHERE id=?",
                (json.dumps(metrics) if metrics is not None else None, deployment_id),
            )

    def save_initial_analysis(self, deployment_id, result):
        from .results import put_initial
        return put_initial(self, deployment_id, result)

    def get_deployment_analysis(self, deployment_id):
        from .results import get_result
        return get_result(self, deployment_id)

    def apply_recovery_analysis(self, deployment_id, result):
        from .results import apply_recovery
        return apply_recovery(self, deployment_id, result)

    def claim_runtime_run(self, deployment_id, revision):
        from .results import claim_run
        return claim_run(self, deployment_id, revision)

    def fail(self, deployment_id):
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE deployments SET status=?, updated_at=? WHERE id=?", (Status.FAILED.value, _now(), deployment_id)
            )
            self._conn.execute(
                "UPDATE deployment_targets SET status=? WHERE deployment_id=?", (Status.FAILED.value, deployment_id)
            )

    def set_status(self, deployment_id, status):
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE deployments SET status=?, updated_at=? WHERE id=?",
                (Status(status).value, _now(), deployment_id),
            )

    def save_plans(self, deployment_id, plans):
        """B Planner의 plan.<target>.json을 배포 작업에 붙인다. 스키마가 틀리면 ValueError."""
        from pydantic import ValidationError

        from schemas.plan import Plan

        try:
            checked = {target: Plan.model_validate(plan) for target, plan in plans.items()}
        except ValidationError as exc:
            raise ValueError(f"invalid plan: {exc.errors()[0]['msg']}") from exc
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO plans VALUES (?, ?, ?)",
                [(deployment_id, target, plan.model_dump_json()) for target, plan in checked.items()],
            )

    def get_plans(self, deployment_id):
        rows = self._all("SELECT target, plan FROM plans WHERE deployment_id=?", (deployment_id,))
        return {r["target"]: json.loads(r["plan"]) for r in rows}

    def last_live(self, project_id, before_deployment_id, with_plans=False):
        """before_deployment_id보다 먼저 만들어진 배포 중 가장 최근 LIVE(롤백 기준점).

        with_plans=True면 plan이 있는 것만 본다(승인 비교 기준 = 지금 돌고 있는 구조).
        """
        has_plans = " AND EXISTS (SELECT 1 FROM plans p WHERE p.deployment_id=d.id)" if with_plans else ""
        row = self._one(
            "SELECT d.* FROM deployments d, deployments ref WHERE ref.id=? AND d.project_id=? AND d.status=? "
            "AND d.rowid < ref.rowid" + has_plans + " ORDER BY d.rowid DESC LIMIT 1",
            (before_deployment_id, project_id, Status.LIVE.value),
        )
        return self._deployment(row) if row else None

    def last_live_plans(self, project_id, before_deployment_id, targets):
        """대상마다, 그 대상이 마지막으로 정상(LIVE)이었던 배포의 설계도(승인 비교 기준).

        배포마다 대상을 고르므로 '직전 배포 전체'가 아니라 대상별 직전 정상 상태와 비교한다.
        """
        rows = self._all(
            "SELECT t.target, p.plan FROM deployment_targets t "
            "JOIN plans p ON p.deployment_id=t.deployment_id AND p.target=t.target "
            "JOIN deployments d ON d.id=t.deployment_id, deployments ref "
            "WHERE ref.id=? AND d.project_id=? AND d.rowid < ref.rowid AND (t.status=? OR d.status=?) ORDER BY d.rowid DESC",
            (before_deployment_id, project_id, Status.LIVE.value, Status.LIVE.value),
        )
        found = {}
        for row in rows:
            if row["target"] in targets and row["target"] not in found:
                found[row["target"]] = json.loads(row["plan"])
        return found

    def last_live_for_targets(self, project_id, before_deployment_id, targets):
        """Newest earlier deployment with successful, stored plans for every requested target.

        Local-only runs must not become an AWS baseline. A successful target
        remains eligible even when a different target made the overall run fail.
        Rollback uses one common revision, so all selected targets must match.
        """
        targets = sorted(set(targets))
        if not targets:
            return None
        placeholders = ",".join("?" for _ in targets)
        row = self._one(
            "SELECT d.* FROM deployments d, deployments ref WHERE ref.id=? AND d.project_id=? "
            "AND ref.project_id=d.project_id AND d.rowid < ref.rowid "
            "AND (SELECT COUNT(*) FROM deployment_targets t JOIN plans p "
            "ON p.deployment_id=t.deployment_id AND p.target=t.target "
            f"WHERE t.deployment_id=d.id AND t.target IN ({placeholders}) "
            "AND (t.status=? OR d.status=?))=? ORDER BY d.rowid DESC LIMIT 1",
            (before_deployment_id, project_id, *targets, Status.LIVE.value, Status.LIVE.value, len(targets)),
        )
        return self._deployment(row) if row else None

    def await_approval(self, deployment_id, reasons):
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE deployments SET status=?, approval_reasons=?, updated_at=? WHERE id=?",
                (Status.AWAITING_APPROVAL.value, json.dumps(reasons, ensure_ascii=False), _now(), deployment_id),
            )
            self._conn.execute(
                "UPDATE deployment_targets SET status=? WHERE deployment_id=?",
                (Status.AWAITING_APPROVAL.value, deployment_id),
            )

    def resolve_approval(self, deployment_id, approved):
        """승인이면 DEPLOYING, 거절이면 FAILED. 승인 대기 상태가 아니면 False."""
        status = Status.DEPLOYING if approved else Status.FAILED
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE deployments SET status=?, approved_at=?, updated_at=? WHERE id=? AND status=?",
                (status.value, _now() if approved else None, _now(), deployment_id, Status.AWAITING_APPROVAL.value),
            )
            if cur.rowcount:
                self._conn.execute(
                    "UPDATE deployment_targets SET status=? WHERE deployment_id=?", (status.value, deployment_id)
                )
            return cur.rowcount == 1

    def create_rollback(self, deployment_id, base):
        """base(직전 LIVE)의 커밋과 plan으로 새 배포 작업을 만든다. 실행은 begin_deploy가 맡는다."""
        source = self.get_deployment(deployment_id)
        targets = sorted(source["targets"]) if source else []
        if (not targets or source["project_id"] != base["project_id"]
                or not set(targets) <= self.get_plans(base["id"]).keys()):
            raise ValueError("rollback_target_mismatch")
        placeholders = ",".join("?" for _ in targets)
        new_id, now = _new_id("d"), _now()
        with self._lock, self._conn:
            self._conn.execute(
                INSERT_DEPLOYMENT,
                (new_id, base["project_id"], Status.CREATED.value, base["commit_sha"], now, now, "rollback", None, None),
            )
            self._conn.execute("UPDATE deployments SET rollback_of=? WHERE id=?", (deployment_id, new_id))
            self._conn.execute(
                "INSERT INTO plans SELECT ?, target, plan FROM plans WHERE deployment_id=? "
                f"AND target IN ({placeholders})", (new_id, base["id"], *targets)
            )
        return new_id

    def add_event(self, deployment_id, event):
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO events (deployment_id, payload) VALUES (?, ?)",
                (deployment_id, json.dumps(event, ensure_ascii=False)),
            )
            return cur.lastrowid

    def list_events(self, deployment_id, after_seq=0):
        rows = self._all(
            "SELECT seq, payload FROM events WHERE deployment_id=? AND seq>? ORDER BY seq",
            (deployment_id, after_seq),
        )
        return [{"seq": r["seq"], "event": json.loads(r["payload"])} for r in rows]

    def record_delivery(self, delivery_id):
        """처음 본 전송 ID면 True, 이미 처리한 ID면 False."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO webhook_deliveries VALUES (?, ?)", (delivery_id, _now())
            )
            return cur.rowcount == 1
