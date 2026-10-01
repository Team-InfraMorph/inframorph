"""Durable one-use deployment recovery claims, separate from JSON correction."""
import json
import os
from pathlib import Path
import sqlite3
import stat
from contextlib import contextmanager


class RetryStore:
    """Use a caller-owned private directory outside the source snapshot.

    Claims are committed before any model/build/run call. A crash leaves the
    claim spent. Control Plane creates a NEW deployment id for a manual retry.
    """

    def __init__(self, path: Path):
        self.path = Path(path).absolute()
        if any(p.is_symlink() for p in (self.path, *self.path.parents)):
            raise ValueError("retry_store_symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        except FileExistsError:
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("retry_store_non_regular")
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS recovery (
                    deployment_id TEXT PRIMARY KEY, source_revision TEXT NOT NULL,
                    snapshot_digest TEXT NOT NULL, attempts INTEGER NOT NULL CHECK (attempts IN (0,1)),
                    status TEXT NOT NULL, reason TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recovery_events (
                    seq INTEGER PRIMARY KEY, deployment_id TEXT NOT NULL, payload TEXT NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def claim(self, deployment_id, revision, digest, allowed):
        # Primary-key insertion is an atomic concurrency guard across processes.
        with self.connect() as db:
            result = db.execute(
                "INSERT OR IGNORE INTO recovery VALUES (?, ?, ?, ?, ?, ?)",
                (deployment_id, revision, digest, int(allowed),
                 "running" if allowed else "stopped", "retry_started" if allowed else "not_retryable"))
            existing = db.execute("SELECT * FROM recovery WHERE deployment_id=?", (deployment_id,)).fetchone()
            if existing[1:3] != (revision, digest):
                raise ValueError("deployment_binding_mismatch")
            return result.rowcount == 1, existing[3]

    def finish(self, deployment_id, status, reason):
        with self.connect() as db:
            db.execute("UPDATE recovery SET status=?, reason=? WHERE deployment_id=?",
                       (status, reason, deployment_id))

    def append(self, event):
        with self.connect() as db:
            db.execute("INSERT INTO recovery_events(deployment_id,payload) VALUES (?,?)",
                       (event.deployment_id, event.model_dump_json(exclude_none=True)))

    def inspect(self, deployment_id):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            record = db.execute("SELECT * FROM recovery WHERE deployment_id=?", (deployment_id,)).fetchone()
            if record is None:
                return None
            data = dict(record)
            data["events"] = [json.loads(row[0]) for row in db.execute(
                "SELECT payload FROM recovery_events WHERE deployment_id=? ORDER BY seq", (deployment_id,))]
            return data
