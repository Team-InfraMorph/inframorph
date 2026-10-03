"""Private bootstrap receipts. Schema checks still run on every deployment."""
import hashlib
import json
import os
import tempfile
from pathlib import Path


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class DatabaseReceipt:
    def __init__(self, path: Path):
        self.path = path
        self.completed = {}
        if path.is_symlink():
            raise ValueError("database_receipt_symlink")
        if path.exists():
            try:
                raw = json.loads(path.read_text())
                if isinstance(raw, dict) and raw.get("version") == 1:
                    self.completed = {key: raw[key] for key in ("bootstrap",)
                                      if isinstance(raw.get(key), str) and len(raw[key]) == 64}
            except (ValueError, OSError):
                pass  # No verifiable receipt: run the tasks again.

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Unique private temporary files also allow recovery after a process crash.
        fd, name = tempfile.mkstemp(prefix=self.path.name + ".", dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump({"version": 1, **self.completed}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def clear(self):
        self.completed = {}
        self.save()

    def run(self, phase, key, execute):
        if phase != "bootstrap":
            raise ValueError("only_database_bootstrap_may_be_reused")
        if self.completed.get(phase) == key:
            return "reused"
        # A failed bootstrap must not retain an older successful receipt.
        self.completed.pop(phase, None)
        self.save()
        execute()
        self.completed[phase] = key
        self.save()
        return "executed"
