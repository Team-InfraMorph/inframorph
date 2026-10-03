"""Private receipt for migrations already applied to the live Cloud SQL database."""
import hashlib
import json
import os
import tempfile
from pathlib import Path


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class MigrationReceipt:
    def __init__(self, path: Path):
        self.path = path
        self.completed = None
        if path.is_symlink():
            raise ValueError("migration_receipt_symlink")
        if path.exists():
            try:
                raw = json.loads(path.read_text())
                if raw.get("version") == 1 and isinstance(raw.get("migration"), str) and len(raw["migration"]) == 64:
                    self.completed = raw["migration"]
            except (ValueError, OSError, AttributeError):
                pass

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=self.path.name + ".", dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump({"version": 1, **({"migration": self.completed} if self.completed else {})}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def run(self, key, execute):
        if self.completed == key:
            return "reused"
        self.completed = None
        self.save()
        execute()
        self.completed = key
        self.save()
        return "executed"
