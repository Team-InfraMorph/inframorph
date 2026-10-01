"""Git push가 재분석을 요구하는지 결정하는 순수 규칙 모듈."""
from dataclasses import dataclass
import shlex
from pathlib import PurePosixPath


DEPENDENCY_FILES = frozenset({
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
})
SOURCE_SUFFIXES = frozenset({".cjs", ".js", ".jsx", ".mjs", ".ts", ".tsx"})
ENV_FILES = frozenset({".env.example", ".env.sample", ".env.template"})


@dataclass(frozen=True)
class ChangeDecision:
    requires_analysis: bool
    changed_files: tuple[str, ...]
    categories: tuple[str, ...]
    reasons: tuple[str, ...]

    def as_dict(self):
        return {
            "requires_analysis": self.requires_analysis,
            "changed_files": list(self.changed_files),
            "categories": list(self.categories),
            "reasons": list(self.reasons),
        }


def _get(value, key, default=None):
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _normalize_path(value):
    if not isinstance(value, str):
        raise ValueError("changed file path must be a string")
    path = value.strip().replace("\\", "/")
    if not path or path.startswith("/"):
        raise ValueError("changed file path must be relative")
    while path.startswith("./"):
        path = path[2:]
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("changed file path must not contain traversal segments")
    return str(PurePosixPath(path))


def _evidence_path(value):
    evidence = _get(value, "at", value)
    if not isinstance(evidence, str):
        return None
    return evidence.split(":", 1)[0]


def _hint_paths(repo_map):
    paths = {}
    for hint in _get(repo_map, "hints", []):
        path = _evidence_path(hint)
        hint_type = _get(hint, "type")
        if path and hint_type:
            paths.setdefault(path, set()).add(hint_type)
    return paths


def _evidence_paths(intent, section):
    """intent의 workloads 또는 state 항목이 근거로 든 파일 경로."""
    paths = set()
    if intent is None:
        return paths
    for item in _get(intent, section, []):
        for evidence in _get(item, "evidence", []):
            path = _evidence_path(evidence)
            if path:
                paths.add(path)
    return paths


def _entrypoint_paths(repo_map):
    paths = set()
    for command in _get(repo_map, "entrypoints", {}).values():
        if not isinstance(command, str):
            continue
        try:
            tokens = shlex.split(command)
        except ValueError:
            continue
        for token in tokens:
            suffix = PurePosixPath(token).suffix.lower()
            if suffix in SOURCE_SUFFIXES:
                try:
                    paths.add(_normalize_path(token))
                except ValueError:
                    continue
    return paths


def detect_changes(changed_files, repo_map, intent=None):
    """Return whether changed files can affect the inferred deployment shape."""
    normalized = tuple(dict.fromkeys(_normalize_path(path) for path in changed_files))
    hints = _hint_paths(repo_map)
    workload_evidence = _evidence_paths(intent, "workloads")
    state_evidence = _evidence_paths(intent, "state")
    entrypoints = _entrypoint_paths(repo_map)
    tree = set(_get(repo_map, "tree", []))
    categories = set()
    reasons = []

    for path in normalized:
        filename = PurePosixPath(path).name
        if filename in DEPENDENCY_FILES:
            categories.add("dependency")
            reasons.append(f"dependency manifest changed: {path}")
        if path.endswith(".prisma") or path.startswith("prisma/"):
            categories.add("schema")
            reasons.append(f"database schema changed: {path}")
        for hint_type in hints.get(path, ()):
            category = "environment" if hint_type == "env" else hint_type
            categories.add(category)
            reasons.append(f"{hint_type} hint changed: {path}")
        if path in workload_evidence or path in entrypoints:
            categories.add("entrypoint")
            reasons.append(f"workload entrypoint changed: {path}")
        if path in state_evidence:
            categories.add("state")
            reasons.append(f"database or persistent file evidence changed: {path}")
        if filename in ENV_FILES:
            categories.add("environment")
            reasons.append(f"environment template changed: {path}")
        if path not in tree and PurePosixPath(path).suffix.lower() in SOURCE_SUFFIXES:
            categories.add("new_source")
            reasons.append(f"new source file added: {path}")

    ordered_categories = tuple(sorted(categories))
    return ChangeDecision(
        requires_analysis=bool(ordered_categories),
        changed_files=normalized,
        categories=ordered_categories,
        reasons=tuple(dict.fromkeys(reasons)),
    )


NULL_SHA = "0" * 40


@dataclass(frozen=True)
class RedeployDecision:
    """push 한 건을 어떤 깊이로 다시 처리할지.

    full_analysis: 이전 분석을 믿을 수 없어 처음부터 분석한다.
    reanalyze:     배포 모양에 영향을 주는 파일이 바뀌어 분석 단계를 다시 돈다.
    rebuild_only:  분석 결과를 재사용하고 이미지만 다시 빌드한다(AI 호출 없음).
    """
    mode: str
    categories: tuple[str, ...]
    reasons: tuple[str, ...]

    def as_dict(self):
        return {"mode": self.mode, "categories": list(self.categories), "reasons": list(self.reasons)}


def plan_redeploy(push, cached):
    """push와 before 커밋의 분석 캐시로 재처리 깊이를 정한다. 확신이 없으면 항상 더 깊은 쪽을 고른다."""
    if cached is None:
        return RedeployDecision("full_analysis", (), ("no cached analysis for previous commit",))
    if push.forced:
        return RedeployDecision("full_analysis", (), ("force push: changed file list is not reliable",))
    if push.before == NULL_SHA:
        return RedeployDecision("full_analysis", (), ("new branch: no previous commit",))
    if push.commit_count == 0 or not push.changed_files:
        return RedeployDecision("full_analysis", (), ("push payload has no changed file list",))

    decision = detect_changes(push.changed_files, cached["repo_map"], cached.get("intent"))
    if decision.requires_analysis:
        return RedeployDecision("reanalyze", decision.categories, decision.reasons)
    return RedeployDecision("rebuild_only", (), ("no deployment-relevant files changed",))
