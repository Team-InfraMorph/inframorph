"""Deterministic MVP gates. No model calls or execution of repository programs."""

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from pydantic import ValidationError

from schemas import Intent, Plan
from schemas.common import check_revision, parse_evidence
from .reporting import checked, bind, rule

EXCLUDED_DIRS = {
    ".git",
    ".aws",
    ".ssh",
    ".codex",
    ".agents",
    "node_modules",
    ".venv",
    "dist",
}
EXCLUDED_FILES = {
    ".npmrc",
    ".pypirc",
    "auth.json",
    "credentials",
    "credentials.json",
    "id_rsa",
    "id_ed25519",
}
DEFAULT_PATHS = frozenset(
    {
        "prisma/schema.prisma",
        "src/images.js",
        "src/storage.js",
        "package.json",
        "package-lock.json",
    }
)
MAX_FILE = 4 * 1024 * 1024
MAX_TREE = 20 * 1024 * 1024


class PolicyError(ValueError):
    """Only fixed diagnostic codes; never echo attacker-controlled content."""


def require(ok, code):
    if not ok:
        raise PolicyError(code)


def eligible(name):
    parts = name.split("/")
    return not (
        any(p in EXCLUDED_DIRS or p.startswith(".env") for p in parts)
        or parts[-1] in EXCLUDED_FILES
        or Path(name).suffix.lower() in {".pem", ".key", ".p12", ".pfx"}
    )


def safe_name(name):
    require(
        isinstance(name, str)
        and bool(name)
        and not name.startswith("/")
        and "\\" not in name
        and all(p not in ("", ".", "..") for p in name.split("/"))
        and not any(ord(c) < 32 for c in name),
        "invalid_path",
    )


def digest(files):
    h = hashlib.sha256()
    for name, data in sorted(files.items()):
        h.update(name.encode() + b"\0" + data + b"\0")
    return h.hexdigest()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read_tree(root, *, filter_source=False):
    """Open each component using directory descriptors and O_NOFOLLOW."""
    root = Path(root)
    files, excluded = {}, []
    total = 0
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

        def visit(fd, prefix=""):
            nonlocal total
            for name in sorted(os.listdir(fd)):
                relative = prefix + name
                safe_name(relative)
                if filter_source and not eligible(relative):
                    excluded.append(relative)
                    continue
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                require(not stat.S_ISLNK(info.st_mode), "symlink_forbidden")
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(
                        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                    )
                    try:
                        visit(child, relative + "/")
                    finally:
                        os.close(child)
                else:
                    require(
                        stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                        "non_regular_file",
                    )
                    require(
                        info.st_size <= MAX_FILE and len(files) < 2000, "source_limit"
                    )
                    file_fd = os.open(
                        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
                    )
                    with os.fdopen(file_fd, "rb") as stream:
                        opened = os.fstat(stream.fileno())
                        require(
                            stat.S_ISREG(opened.st_mode) and opened.st_nlink == 1,
                            "non_regular_file",
                        )
                        data = stream.read(MAX_FILE + 1)
                    total += len(data)
                    require(len(data) <= MAX_FILE and total <= MAX_TREE, "source_limit")
                    require(b"\0" not in data, "binary_source_unsupported")
                    data.decode("utf-8")
                    files[relative] = data

        try:
            visit(root_fd)
        finally:
            os.close(root_fd)
    except (OSError, UnicodeError):
        raise PolicyError("unreadable_source") from None
    return files, excluded


def model(cls, value):
    try:
        return cls.model_validate(value.model_dump(mode="json") if isinstance(value, cls) else value)
    except (ValidationError, ValueError, TypeError):
        raise PolicyError("schema_invalid") from None


@checked("intent")
def validate_intent(value, snapshot, source_revision):
    with rule('I-000') as evidence:
        try:
            check_revision(source_revision)
        except ValueError:
            raise PolicyError("revision_invalid") from None
        intent = model(Intent, value)
        require(intent.source_revision == source_revision, "revision_mismatch")
        require(not intent.unknowns, "unresolved_intent")
        files, _ = read_tree(snapshot, filter_source=True)
        bind(snapshot_digest=digest(files), source_revision=source_revision)
        evidence.update(source_revision=source_revision,file_count=len(files),input_digest=digest(files))
    with rule('I-001') as evidence:
        evidence["references"] = [e for x in [*intent.workloads,*intent.state] for e in x.evidence]
        for entity in [*intent.workloads, *intent.state]:
            for evidence in entity.evidence:
                name, line = parse_evidence(evidence)
                require(name in files, "evidence_file_missing")
                require(
                    line <= len(files[name].decode().splitlines()), "evidence_line_missing"
                )
    from .rules import intent_rules
    intent_rules(intent, files)
    return intent


def write_files(root, files):
    for name, data in files.items():
        safe_name(name)
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def clean_env():
    return {
        k: v
        for k, v in os.environ.items()
        if k
        in (
            "PATH",
            "HOME",
            "USER",
            "LOGNAME",
            "TMPDIR",
            "DOCKER_CONFIG",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
        )
    }


def syntax_check(files):
    with tempfile.TemporaryDirectory(prefix="gate-syntax-") as directory:
        root = Path(directory)
        write_files(root, files)
        for name in files:
            if name.endswith((".js", ".cjs", ".mjs")):
                try:
                    result = subprocess.run(
                        ["node", "--check", str(root / name)],
                        capture_output=True,
                        timeout=15,
                        env=clean_env(),
                    )
                except (OSError, subprocess.SubprocessError):
                    raise PolicyError("syntax_checker_unavailable") from None
                require(result.returncode == 0, "javascript_syntax_invalid")


@dataclass(frozen=True)
class VerifiedArtifact:
    source_revision: str
    target: str
    original_digest: str
    patched_digest: str
    diff_sha256: str
    files: object


@checked("patch")
def validate_patch(snapshot, bundle, plan, *, allowed_paths=DEFAULT_PATHS, profile="reviewed"):
    with rule('P-001') as evidence:
        plan = model(Plan, plan)
        original, _ = read_tree(snapshot, filter_source=True)
        all_bundle, _ = read_tree(bundle)
        require(
            "manifest.json" in all_bundle and "patch.diff" in all_bundle,
            "bundle_incomplete",
        )
        require(
            all(
                name in ("manifest.json", "patch.diff") or name.startswith("source/")
                for name in all_bundle
            ),
            "bundle_extra_file",
        )
        try:
            report = json.loads(all_bundle["manifest.json"])
        except (ValueError, UnicodeError):
            raise PolicyError("manifest_invalid") from None
        require(isinstance(report, dict), "manifest_invalid")
        require(report.get("schema_version") == "1.0.0", "manifest_version_unsupported")
        patched = {
            name[7:]: data
            for name, data in all_bundle.items()
            if name.startswith("source/")
        }
        require(
            patched and all(eligible(name) for name in patched), "sensitive_source_path"
        )
        require(
            report.get("source_revision") == plan.source_revision
            and report.get("target") == plan.target.value,
            "revision_or_target_mismatch",
        )
        require(report.get("requires_policy_gate") is True, "manifest_gate_flag_missing")
        patch = all_bundle["patch.diff"]
        require(
            report.get("original_digest") == digest(original)
            and report.get("patched_digest") == digest(patched)
            and report.get("diff_sha256") == sha(patch),
            "artifact_digest_mismatch",
        )
        changed = sorted(
            name
            for name in original.keys() | patched.keys()
            if original.get(name) != patched.get(name)
        )
        evidence.update(changed_paths=changed,source_revision=plan.source_revision,target=plan.target.value,input_digest=digest(original))
        require(set(changed) <= set(allowed_paths), "patch_allowlist_violation")
        require(all(name in patched for name in changed), "deletion_forbidden")
        expected = [
            {
                "path": name,
                "action": "modify" if name in original else "add",
                "before_sha256": sha(original[name]) if name in original else None,
                "after_sha256": sha(patched[name]),
            }
            for name in changed
        ]
        require(report.get("changes") == expected, "change_manifest_mismatch")
        require(
            report.get("status") == ("patched" if changed else "unchanged"),
            "patch_status_mismatch",
        )
    with rule('P-006') as evidence:
        from .structure import inspect_js
        js = inspect_js(patched)
        evidence.update(file_count=len(js))
        for name, parsed in js.items():
            if parsed.get("error"):
                error = PolicyError(parsed["error"]); error.path = name; raise error
            if parsed["forbidden"]:
                error = PolicyError("forbidden_code_pattern"); error.path = name; raise error
    with rule('P-007') as evidence:
        evidence.update(file_count=len(patched))
        for name, data in patched.items():
            require(
                not re.search(
                    rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bAKIA[0-9A-Z]{16}\b|\bgh[pousr]_[A-Za-z0-9]{30,}",
                    data,
                ),
                "secret_in_source",
            )
    with rule('P-002') as evidence:
        evidence.update(input_digest=sha(patch))
        # Compare the actual effect of diff with the supplied source tree; not just two claimed hashes.
        with tempfile.TemporaryDirectory(prefix="gate-apply-") as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            write_files(source, original)
            patch_file = root / "patch.diff"
            patch_file.write_bytes(patch)
            try:
                subprocess.run(
                    ["git", "init", "--quiet", str(source)],
                    check=True,
                    capture_output=True,
                    timeout=15,
                    env=clean_env(),
                )
                if patch:
                    result = subprocess.run(
                        ["git", "apply", "--whitespace=nowarn", str(patch_file)],
                        cwd=source,
                        capture_output=True,
                        timeout=15,
                        env=clean_env(),
                    )
                    require(result.returncode == 0, "diff_apply_failed")
                applied, _ = read_tree(source, filter_source=True)
                require(applied == patched, "diff_source_mismatch")
            except (OSError, subprocess.SubprocessError):
                raise PolicyError("diff_checker_unavailable") from None
    from .rules import patch_rules
    patch_rules(original, patched, plan, changed, js, profile=profile)
    return VerifiedArtifact(
        plan.source_revision,
        plan.target.value,
        digest(original),
        digest(patched),
        sha(patch),
        MappingProxyType(dict(patched)),
    )


@checked("plan")
def validate_plan(intent, value):
    from .rules import plan_rules
    with rule('L-001') as evidence:
        intent, plan = model(Intent, intent), model(Plan, value)
        evidence.update(source_revision=plan.source_revision,target=plan.target.value)
        bind(intent_digest=sha(intent.model_dump_json().encode()), plan_digest=sha(plan.model_dump_json().encode()), source_revision=plan.source_revision)
    with rule('L-002') as evidence:
        evidence.update(services=[s.name for s in plan.services],public_services=[s.name for s in plan.services if s.public])
        plan_rules(intent, plan)
    return plan
