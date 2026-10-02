"""Copy a verified Git commit into an isolated, read-only source tree."""

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile

MAX_FILES = 2000
MAX_FILE = 4 * 1024 * 1024
MAX_TOTAL = 20 * 1024 * 1024
REQUIRED = {
    "package.json", "package-lock.json", "prisma/schema.prisma",
    "src/images.js", "src/server.js",
}


@dataclass(frozen=True)
class Snapshot:
    commit: str
    path: Path
    files: dict[str, bytes]


def git(repo, *args):
    env = dict(os.environ)
    env.update(
        GIT_TERMINAL_PROMPT="0",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_LFS_SKIP_SMUDGE="1",
    )
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=90, env=env,
    ).stdout


def _safe_name(raw_name):
    name = raw_name.decode("utf-8")
    parts = PurePosixPath(name).parts
    if (not name or name.startswith("/") or "\\" in name
            or any(part in {"", ".", ".."} for part in name.split("/"))
            or ".git" in parts):
        raise ValueError("unsafe_source_path")
    return name


def snapshot_from_checkout(repo, source_revision, output_dir):
    """Read blobs from a commit without checking out or executing source code."""
    repo = Path(repo)
    output = Path(output_dir).absolute()
    if output.is_symlink() or not output.is_dir():
        raise ValueError("invalid_output_dir")
    output = output.resolve(strict=True)

    snapshot = output / "snapshot"
    if snapshot.exists() or snapshot.is_symlink():
        raise ValueError("output_exists")
    if source_revision is not None and (
        not isinstance(source_revision, str)
        or not re.fullmatch(r"[0-9a-f]{40}", source_revision)
    ):
        raise ValueError("invalid_revision")

    revision = git(
        repo, "rev-parse", "--verify", f"{source_revision or 'HEAD'}^{{commit}}"
    ).decode("ascii").strip()
    if source_revision is not None and revision != source_revision:
        raise ValueError("revision_mismatch")

    entries = [entry for entry in git(
        repo, "ls-tree", "-r", "-l", "-z", revision
    ).split(b"\0") if entry]
    if len(entries) > MAX_FILES:
        raise ValueError("too_many_files")

    files = {}
    total = 0
    for entry in entries:
        meta, raw_name = entry.split(b"\t", 1)
        name = _safe_name(raw_name)
        mode, kind, oid, raw_size = meta.decode("ascii").split()
        # Symlinks, submodules, and binary or huge blobs need a separate policy.
        if mode not in {"100644", "100755"} or kind != "blob":
            raise ValueError("unsupported_file_type")
        size = int(raw_size)
        total += size
        if size > MAX_FILE or total > MAX_TOTAL:
            raise ValueError("source_too_large")
        data = git(repo, "cat-file", "blob", oid)
        if len(data) != size or b"\0" in data:
            raise ValueError("invalid_source_file")
        data.decode("utf-8")
        files[name] = data

    if not REQUIRED <= files.keys():
        raise ValueError("missing_demo_files")

    snapshot.mkdir(mode=0o700)
    try:
        for name, data in files.items():
            path = snapshot / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o444)
    except Exception:
        shutil.rmtree(snapshot)
        raise
    return Snapshot(commit=revision, path=snapshot, files=files)


def snapshot_from_remote(repo_url, branch, source_revision, output_dir):
    """Clone into a disposable workspace and verify revision is on branch."""
    with tempfile.TemporaryDirectory(prefix="inframorph-clone-") as temporary:
        repo = Path(temporary) / "repo"
        # A full clone intentionally avoids a shallow-history SHA mismatch.
        git(
            temporary, "clone", "--quiet", "--no-checkout", "--single-branch",
            "--branch", branch, repo_url, str(repo),
        )
        if source_revision is not None:
            git(repo, "merge-base", "--is-ancestor", source_revision, "HEAD")
        return snapshot_from_checkout(repo, source_revision, output_dir)
