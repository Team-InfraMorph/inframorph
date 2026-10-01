"""Bounded in-memory snapshot. Tools never run source files or shell commands."""
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import stat

from schemas.common import check_relative_path

from .config import Limits
from .redaction import Redactor


class SnapshotError(ValueError):
    pass


EXCLUDED_DIRS = {".git", ".aws", ".ssh", ".codex", ".agents", "node_modules", ".venv", "dist"}
EXCLUDED_FILES = {".npmrc", ".pypirc", "auth.json", "credentials", "credentials.json", "id_rsa", "id_ed25519"}


def eligible(path: str) -> bool:
    parts = path.split("/")
    return not (
        any(part in EXCLUDED_DIRS or part.startswith(".env") for part in parts)
        or parts[-1] in EXCLUDED_FILES
        or Path(path).suffix.lower() in {".pem", ".key", ".p12", ".pfx"}
    )


def _read_at(root_fd: int, path: str, limit: int) -> bytes:
    """Open each component without following symlinks, including concurrent swaps."""
    directory = os.dup(root_fd)
    try:
        parts = path.split("/")
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SnapshotError("snapshot_non_regular_file")
            if info.st_size > limit:
                raise SnapshotError("snapshot_file_too_large")
            data = source.read(limit + 1)
            if len(data) > limit:
                raise SnapshotError("snapshot_file_too_large")
            return data
    finally:
        os.close(directory)


class Snapshot:
    def __init__(self, root: Path, tree: list[str], limits: Limits, redactor: Redactor):
        self.limits = limits
        self.redactor = redactor
        self.files: dict[str, list[str]] = {}
        self.observed: set[tuple[str, int]] = set()
        self.excluded: list[str] = []
        if len(tree) > limits.max_files or len(tree) != len(set(tree)):
            raise SnapshotError("snapshot_invalid_file_count")
        for path in tree:
            try:
                check_relative_path(path)
            except ValueError:
                raise SnapshotError("snapshot_invalid_path") from None
            if path.endswith("/") or "\x00" in path:
                raise SnapshotError("snapshot_invalid_path")
        total = 0
        digest = hashlib.sha256()
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for path in sorted(tree):
                if not eligible(path):
                    self.excluded.append(path)
                    continue
                try:
                    data = _read_at(root_fd, path, limits.max_file_bytes)
                    text = data.decode("utf-8")
                    if "\x00" in text:
                        raise SnapshotError("snapshot_binary_file")
                except (OSError, UnicodeError):
                    raise SnapshotError("snapshot_unreadable_file") from None
                total += len(data)
                if total > limits.max_snapshot_bytes:
                    raise SnapshotError("snapshot_too_large")
                digest.update(path.encode() + b"\0" + data + b"\0")
                self.files[path] = redactor.clean(text).splitlines()
        finally:
            os.close(root_fd)
        # Later files may reveal a secret also present in an earlier file.
        self.files = {path: [redactor.clean(line) for line in lines] for path, lines in self.files.items()}
        self.digest = digest.hexdigest()

    def dispatch(self, name: str, arguments: dict) -> dict:
        if name == "Read":
            path, start, count = arguments["path"], arguments["start_line"], arguments["line_count"]
            if path not in self.files:
                raise SnapshotError("file_not_available")
            if start > len(self.files[path]):
                raise SnapshotError("line_out_of_range")
            rows = [(path, i, line) for i, line in enumerate(self.files[path], 1) if start <= i < start + count]
            result = self._rows(rows)
            result["total_lines"] = len(self.files[path])
            result["has_more"] = start - 1 + len(result["lines"]) < len(self.files[path])
            return result
        if name == "Glob":
            matches = sorted(path for path in self.files if _match(path, arguments["pattern"]))
            return {"paths": matches[:100], "truncated": len(matches) > 100}
        if name == "Grep":
            # Literal search avoids regex denial-of-service on untrusted patterns.
            rows = [(path, i, line) for path, lines in sorted(self.files.items())
                    if _match(path, arguments["glob"])
                    for i, line in enumerate(lines, 1) if arguments["text"] in line]
            return self._rows(rows)
        raise SnapshotError("tool_not_allowed")

    def _rows(self, rows: list[tuple[str, int, str]]) -> dict:
        selected = []
        size = 0
        for path, number, text in rows:
            row = {"path": path, "line": number, "text": text}
            size += len(json.dumps(row, ensure_ascii=True).encode())
            if size > self.limits.max_tool_output_bytes or len(selected) >= 200:
                break
            selected.append(row)
            self.observed.add((path, number))
        return {"lines": selected, "truncated": len(selected) < len(rows)}


def _match(path: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(path, pattern) or (pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:]))
