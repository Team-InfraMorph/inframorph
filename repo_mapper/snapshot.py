"""Fetch one exact commit and copy its deployable files into a read-only snapshot.

Git objects are read with ls-tree/cat-file; no working tree is checked out, so
repository hooks, filters, LFS, submodules and symlinks never run or resolve.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile

from analyzer.config import Limits
from analyzer.redaction import Redactor
from analyzer.snapshot import eligible
from schemas.common import check_relative_path

from .errors import MapperError


SHA = re.compile(r"[0-9a-f]{40}")
GIT_TIMEOUT = 60
# Deployable inputs only. Tooling (.github, scripts, tests, dotfiles) is not
# copied, so downstream source policy never sees unreviewed executable files.
ROOT_FILES = frozenset({"package.json", "package-lock.json"})
SOURCE_DIRS = ("prisma/", "src/")


def selected(path: str) -> bool:
    return (path in ROOT_FILES or path.startswith(SOURCE_DIRS)) and eligible(path)


@dataclass(frozen=True)
class Snapshot:
    commit: str
    root: Path
    files: dict[str, bytes]


def _git_env() -> dict[str, str]:
    env = {key: os.environ[key] for key in ("PATH", "TMPDIR", "LANG", "SSL_CERT_FILE", "SSL_CERT_DIR")
           if key in os.environ}
    # Ignore user/system config (credential helpers, url rewrites, hooksPath).
    env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_LFS_SKIP_SMUDGE="1", GIT_ASKPASS="", SSH_ASKPASS="")
    return env


def _git(args, *, cwd=None, code="clone_failed", data=None) -> bytes:
    try:
        result = subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull, "-c", "protocol.file.allow=always",
                                 *args], cwd=cwd, input=data, capture_output=True, env=_git_env(),
                                timeout=GIT_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise MapperError(code) from None
    if result.returncode:
        raise MapperError(code)
    return result.stdout


def resolve_branch(remote: str, branch: str) -> str:
    # Unreachable/private repository fails here; a missing branch returns no rows.
    out = _git(["ls-remote", "--heads", "--", remote, "refs/heads/" + branch], code="clone_failed")
    rows = [line.split("\t") for line in out.decode().splitlines() if line]
    matches = [sha for sha, ref in rows if ref == "refs/heads/" + branch]
    if len(matches) != 1 or not SHA.fullmatch(matches[0]):
        raise MapperError("branch_not_found")
    return matches[0]


def _fetch(work: Path, remote: str, revision: str) -> None:
    _git(["init", "-q", "--bare", str(work)])
    try:
        _git(["fetch", "-q", "--depth", "1", "--no-tags", "--", remote, revision], cwd=work,
             code="revision_not_found")
    except MapperError:
        # Servers that refuse SHA wants still expose branch heads.
        _git(["fetch", "-q", "--no-tags", "--", remote, "+refs/heads/*:refs/remotes/origin/*"], cwd=work,
             code="revision_not_found")
    resolved = _git(["rev-parse", "--verify", "--end-of-options", revision + "^{commit}"], cwd=work,
                    code="revision_not_found").decode().strip()
    if resolved != revision:
        raise MapperError("revision_mismatch")


def _entries(work: Path, revision: str, limits: Limits) -> list[tuple[str, str, int]]:
    out = _git(["ls-tree", "-r", "-l", "-z", "--full-tree", revision], cwd=work, code="revision_not_found")
    entries = []
    for row in out.split(b"\0"):
        if not row:
            continue
        meta, _, raw_path = row.partition(b"\t")
        mode, kind, obj, size = meta.decode().split()
        try:
            path = raw_path.decode("utf-8")
            check_relative_path(path)
        except (UnicodeError, ValueError):
            raise MapperError("path_rejected") from None
        if any(ord(c) < 32 for c in path):
            raise MapperError("path_rejected")
        if not selected(path):
            continue
        if mode == "120000" or kind != "blob":
            raise MapperError("symlink_rejected" if mode == "120000" else "submodule_rejected")
        if int(size) > limits.max_file_bytes:
            raise MapperError("snapshot_limit")
        entries.append((path, obj, int(size)))
    if len(entries) > limits.max_files or sum(size for *_, size in entries) > limits.max_snapshot_bytes:
        raise MapperError("snapshot_limit")
    if "package.json" not in {path for path, *_ in entries}:
        raise MapperError("unsupported_repository")
    return sorted(entries)


def _read(work: Path, entries) -> dict[str, bytes]:
    files, redactor = {}, Redactor()
    for path, obj, size in entries:
        data = _git(["cat-file", "blob", obj], cwd=work, code="revision_not_found")
        if len(data) != size:
            raise MapperError("snapshot_limit")
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            raise MapperError("unsupported_file") from None
        if "\x00" in text:
            raise MapperError("unsupported_file")
        if redactor.contains_secret(text):
            raise MapperError("secret_detected")
        files[path] = data
    return files


def _write(root: Path, files: dict[str, bytes]) -> None:
    root.mkdir(mode=0o700)
    directories = {root}
    for path, data in files.items():
        target = root / path
        for parent in reversed(target.relative_to(root).parents[:-1]):
            folder = root / parent
            if folder not in directories:
                folder.mkdir(mode=0o700)
                directories.add(folder)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(data)
        os.chmod(target, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


def create_snapshot(remote: str, revision: str, output_dir: Path, *, limits: Limits | None = None) -> Snapshot:
    """Write output_dir/snapshot for exactly `revision`; nothing is written on failure."""
    limits = limits or Limits()
    root = output_dir / "snapshot"
    if root.exists() or root.is_symlink():
        raise MapperError("invalid_output_dir")
    work = Path(tempfile.mkdtemp(prefix="inframorph-mapper-"))
    try:
        _fetch(work / "repo.git", remote, revision)
        entries = _entries(work / "repo.git", revision, limits)
        files = _read(work / "repo.git", entries)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    try:
        _write(root, files)
    except OSError:
        shutil.rmtree(root, ignore_errors=True)
        raise MapperError("snapshot_write_failed") from None
    return Snapshot(revision, root, files)
