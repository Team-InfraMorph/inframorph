"""GitHub webhook 서명 검증과 push 페이로드 해석."""
import hashlib
import hmac
import json
from dataclasses import dataclass
from urllib.parse import parse_qs

from .change_detector import _normalize_path


@dataclass(frozen=True)
class PushEvent:
    repo_key: str
    branch: str
    before: str
    after: str
    changed_files: tuple[str, ...]
    forced: bool
    commit_count: int = 0
    deleted: bool = False


def verify_signature(secret, body, header):
    """X-Hub-Signature-256 헤더를 원본 본문으로 검증한다. 시크릿이 비어 있으면 항상 거부한다."""
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def normalize_repo_url(url):
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    return url.lower()


def load_payload(body, content_type):
    """GitHub은 JSON 본문 또는 form의 `payload` 필드로 보낸다. 서명은 두 경우 모두 원본 본문 기준이다."""
    if content_type.startswith("application/x-www-form-urlencoded"):
        fields = parse_qs(body.decode("utf-8"))
        if "payload" not in fields:
            raise ValueError("form body has no payload field")
        return json.loads(fields["payload"][0])
    return json.loads(body)


def is_tag_push(payload):
    return isinstance(payload, dict) and str(payload.get("ref", "")).startswith("refs/tags/")


def parse_push(payload):
    """push 페이로드에서 (repo_url, branch, commit)을 꺼낸다. 형식이 틀리면 ValueError."""
    event = parse_push_details(payload)
    return event.repo_key, event.branch, event.after


def parse_push_details(payload):
    """push 페이로드에서 변경 범위와 이전·현재 커밋을 검증해 반환한다."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    ref = payload.get("ref")
    before = payload.get("before")
    after = payload.get("after")
    repo = payload.get("repository")
    if not isinstance(ref, str) or not ref.startswith("refs/heads/"):
        raise ValueError("ref must be a branch ref")
    for name, value in (("before", before), ("after", after)):
        if not isinstance(value, str) or len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError(f"{name} must be a full lowercase commit SHA")
    if not isinstance(repo, dict) or not isinstance(repo.get("html_url"), str):
        raise ValueError("repository.html_url missing")

    commits = payload.get("commits", [])
    if commits is None:
        commits = []
    if not isinstance(commits, list):
        raise ValueError("commits must be a list")
    changed_files = []
    for commit in commits:
        if not isinstance(commit, dict):
            raise ValueError("commit entries must be objects")
        for field in ("added", "modified", "removed"):
            paths = commit.get(field, [])
            if not isinstance(paths, list):
                raise ValueError(f"commit.{field} must be a list")
            changed_files.extend(_normalize_path(path) for path in paths)

    return PushEvent(
        repo_key=normalize_repo_url(repo["html_url"]),
        branch=ref.removeprefix("refs/heads/"),
        before=before,
        after=after,
        changed_files=tuple(dict.fromkeys(changed_files)),
        forced=bool(payload.get("forced", False)),
        commit_count=len(commits),
        deleted=bool(payload.get("deleted", False)) or after == "0" * 40,
    )
