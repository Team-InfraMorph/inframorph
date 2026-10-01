"""SQLite 저장소. 화면은 항상 DB에서 읽으므로 새로고침·재시작 후에도 상태가 유지된다."""
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


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
RUNNING = {Status.DEPLOYING}

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
}
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
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            existing = {row[1] for row in self._conn.execute("PRAGMA table_info(deployments)")}
            for name, ddl in DEPLOYMENT_COLUMNS.items():
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE deployments ADD COLUMN {name} {ddl}")

    def close(self):
        self._conn.close()

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
        row = self._conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            return None
        latest = self._conn.execute(
            "SELECT id FROM deployments WHERE project_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        return {
            "project_id": row["id"],
            "repo_url": row["repo_url"],
            "branch": row["branch"],
            "targets": json.loads(row["targets"]),
            "created_at": row["created_at"],
            "latest_deployment_id": latest["id"] if latest else None,
        }

    def find_projects(self, repo_key, branch):
        rows = self._conn.execute(
            "SELECT id FROM projects WHERE repo_key=? AND branch=? ORDER BY created_at", (repo_key, branch)
        ).fetchall()
        return [r["id"] for r in rows]

    def _deployment(self, row):
        data = dict(row)
        data["change_reasons"] = json.loads(data["change_reasons"]) if data.get("change_reasons") else []
        targets = self._conn.execute(
            "SELECT target, status, url FROM deployment_targets WHERE deployment_id=? ORDER BY target", (row["id"],)
        ).fetchall()
        data["targets"] = {t["target"]: {"status": t["status"], "url": t["url"]} for t in targets}
        return data

    def get_deployment(self, deployment_id):
        row = self._conn.execute("SELECT * FROM deployments WHERE id=?", (deployment_id,)).fetchone()
        return self._deployment(row) if row else None

    def list_deployments(self, project_id):
        rows = self._conn.execute(
            "SELECT * FROM deployments WHERE project_id=? ORDER BY created_at DESC, rowid DESC", (project_id,)
        ).fetchall()
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
        row = self._conn.execute(
            "SELECT repo_map, intent FROM analyses WHERE project_id=? AND commit_sha=?", (project_id, commit_sha)
        ).fetchone()
        if row is None:
            return None
        return {"repo_map": json.loads(row["repo_map"]),
                "intent": json.loads(row["intent"]) if row["intent"] else None}

    def has_queued(self, project_id):
        latest = self._conn.execute(
            "SELECT status FROM deployments WHERE project_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        return latest is not None and latest["status"] == Status.CREATED.value

    def begin_deploy(self, project_id):
        """진행 중 배포가 없을 때만 DEPLOYING으로 바꾼다. 확인과 변경을 한 잠금 안에서 해서 동시 요청을 막는다.

        가장 최근 CREATED 작업을 실행하고, 그보다 오래된 CREATED 작업은 SUPERSEDED로 닫는다.
        대상(local·aws)마다 상태 행을 만든다.
        """
        with self._lock, self._conn:
            if self._conn.execute(
                "SELECT 1 FROM deployments WHERE project_id=? AND status=?",
                (project_id, Status.DEPLOYING.value),
            ).fetchone():
                raise ConflictError(project_id)
            latest = self._conn.execute(
                "SELECT id, status FROM deployments WHERE project_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            now = _now()
            if latest and latest["status"] == Status.CREATED.value:
                deployment_id = latest["id"]
            else:
                deployment_id = _new_id("d")
                self._conn.execute(
                    INSERT_DEPLOYMENT,
                    (deployment_id, project_id, Status.CREATED.value, None, now, now, "manual", None, None),
                )
            self._conn.execute(
                "UPDATE deployments SET status=?, updated_at=? WHERE project_id=? AND status=? AND id<>?",
                (Status.SUPERSEDED.value, now, project_id, Status.CREATED.value, deployment_id),
            )
            self._conn.execute(
                "UPDATE deployments SET status=?, updated_at=? WHERE id=?",
                (Status.DEPLOYING.value, now, deployment_id),
            )
            targets = json.loads(self._conn.execute(
                "SELECT targets FROM projects WHERE id=?", (project_id,)).fetchone()["targets"])
            self._conn.executemany(
                "INSERT OR REPLACE INTO deployment_targets VALUES (?, ?, ?, NULL)",
                [(deployment_id, target, Status.DEPLOYING.value) for target in targets],
            )
        return deployment_id

    def set_target_status(self, deployment_id, target, status, url=None):
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE deployment_targets SET status=?, url=COALESCE(?, url) WHERE deployment_id=? AND target=?",
                (Status(status).value, url, deployment_id, target),
            )

    def set_status(self, deployment_id, status):
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE deployments SET status=?, updated_at=? WHERE id=?",
                (Status(status).value, _now(), deployment_id),
            )

    def add_event(self, deployment_id, event):
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO events (deployment_id, payload) VALUES (?, ?)",
                (deployment_id, json.dumps(event, ensure_ascii=False)),
            )
            return cur.lastrowid

    def list_events(self, deployment_id, after_seq=0):
        rows = self._conn.execute(
            "SELECT seq, payload FROM events WHERE deployment_id=? AND seq>? ORDER BY seq",
            (deployment_id, after_seq),
        ).fetchall()
        return [{"seq": r["seq"], "event": json.loads(r["payload"])} for r in rows]

    def record_delivery(self, delivery_id):
        """처음 본 전송 ID면 True, 이미 처리한 ID면 False."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO webhook_deliveries VALUES (?, ?)", (delivery_id, _now())
            )
            return cur.rowcount == 1
