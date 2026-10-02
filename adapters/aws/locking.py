import fcntl
import os
from pathlib import Path
from typing import IO, Optional

from .errors import DeploymentError


class AppLock:
    """Serialize one app's full pipeline on the single Control Plane host."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Optional[IO[str]] = None

    def __enter__(self) -> "AppLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        os.chmod(str(self.path), 0o600)
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.handle.close()
            self.handle = None
            raise DeploymentError("another deployment for this app is already running") from exc
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(str(os.getpid()) + "\n")
        self.handle.flush()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None
