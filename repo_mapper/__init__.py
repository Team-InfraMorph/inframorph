"""Repo Mapper: exact-commit read-only snapshot plus rule-based repo_map."""
from dataclasses import dataclass
from pathlib import Path
import re

from schemas import RepoMap

from .errors import MapperError
from .rules import build_repo_map
from .snapshot import SHA, create_snapshot, resolve_branch


# Same accepted forms as control_plane/app.py ProjectIn.
REPO_URL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?")
BRANCH = re.compile(r"[A-Za-z0-9._/-]{1,100}")
REQUEST_KEYS = {"repo_url", "branch", "source_revision", "output_dir"}

__all__ = ["MapperError", "MappedRepository", "build_repo_map", "map_repository"]


@dataclass(frozen=True)
class MappedRepository:
    snapshot: Path
    repo_map: RepoMap


def normalize_repo_url(value) -> str:
    match = REPO_URL.fullmatch(value) if isinstance(value, str) else None
    if match is None or match[1] in {".", ".."} or match[2] in {".", ".."}:
        raise MapperError("invalid_request")
    return f"https://github.com/{match[1]}/{match[2]}"


def check_branch(value) -> str:
    if (not isinstance(value, str) or not BRANCH.fullmatch(value) or ".." in value or
            value.startswith(("/", "-")) or value.endswith(("/", ".lock"))):
        raise MapperError("invalid_request")
    return value


def check_output_dir(value) -> Path:
    if not isinstance(value, str) or "\x00" in value:
        raise MapperError("invalid_request")
    path = Path(value)
    if (not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)) or
            not path.is_dir()):
        raise MapperError("invalid_output_dir")
    return path


def map_repository(request: dict, *, remote: str | None = None) -> MappedRepository:
    """Handle one control-plane request.

    `remote` overrides the fetch location for offline tests only; the CLI always
    fetches the normalized GitHub URL.
    """
    if not isinstance(request, dict) or set(request) != REQUEST_KEYS:
        raise MapperError("invalid_request")
    url = normalize_repo_url(request["repo_url"])
    branch = check_branch(request["branch"])
    revision = request["source_revision"]
    if revision is not None and (not isinstance(revision, str) or not SHA.fullmatch(revision)):
        raise MapperError("invalid_request")
    output = check_output_dir(request["output_dir"])
    remote = remote or url
    revision = revision or resolve_branch(remote, branch)
    snapshot = create_snapshot(remote, revision, output)
    return MappedRepository(snapshot.root, build_repo_map(snapshot.commit, snapshot.files))
