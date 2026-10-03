"""No model, shell, package installation or network access inside the patcher.

The first supported profile is the reviewed demo-app v1/v2 source shape. Unknown
source shapes fail closed; this is not a general JavaScript/Prisma transformer.
"""
import difflib
import hashlib
import json
import os
from pathlib import Path
import tempfile

from analyzer.config import Limits
from analyzer.redaction import Redactor
from analyzer.snapshot import _read_at, eligible
from schemas import Plan, RepoMap
from schemas.common import check_relative_path


TEMPLATES = Path(__file__).with_name("templates")
SDK_VERSION = "3.1144.0"
BASE_IMAGES = "1804feff4814858f128b5b89ea0da32602f572a0eca910f8ab55b800310731e4"
BASE_PRISMA = "eaf50f27b33e838a559239fef9a16eaeffdd08081a02d615e03d343f0dc5656c"
BASE_DEPENDENCIES = {"@prisma/client": "6.19.3", "dotenv": "16.6.1", "express": "4.22.3"}
ALLOWED_PATHS = frozenset({"prisma/schema.prisma", "src/images.js", "src/storage.js",
                           "package.json", "package-lock.json"})


class PatchError(ValueError):
    """Fixed diagnostic code; never include raw source or secret values."""


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tree_digest(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name, data in sorted(files.items()):
        digest.update(name.encode() + b"\0" + data + b"\0")
    return digest.hexdigest()


def read_snapshot(root: Path, mapping: RepoMap) -> tuple[dict[str, bytes], list[str]]:
    limits = Limits()
    if len(mapping.tree) > limits.max_files or len(set(mapping.tree)) != len(mapping.tree):
        raise PatchError("invalid_file_count")
    for name in mapping.tree:
        check_relative_path(name)
        if name.endswith("/") or any(ord(c) < 32 for c in name):
            raise PatchError("invalid_path")
    # A copy must not silently lose source files omitted by its Repo Mapper.
    for directory, dirs, names in os.walk(root, followlinks=False):
        base = Path(directory).relative_to(root)
        dirs[:] = [d for d in dirs if eligible((base / d / "file").as_posix())]
        if any((Path(directory) / d).is_symlink() for d in dirs):
            raise PatchError("snapshot_symlink")
        for name in names:
            relative = (base / name).as_posix()
            if eligible(relative) and relative not in mapping.tree:
                raise PatchError("unmapped_source_file")
    files, excluded = {}, []
    total = 0
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in sorted(mapping.tree):
            if not eligible(name):
                excluded.append(name)
                continue
            data = _read_at(fd, name, limits.max_file_bytes)
            text = data.decode("utf-8")
            if "\x00" in text:
                raise PatchError("binary_source_unsupported")
            if Redactor().contains_secret(text):
                raise PatchError("secret_in_source")
            total += len(data)
            if total > limits.max_snapshot_bytes:
                raise PatchError("snapshot_too_large")
            files[name] = data
    finally:
        os.close(fd)
    return files, excluded


def transform(original: dict[str, bytes], plan: Plan) -> dict[str, bytes]:
    patched = dict(original)
    if plan.db:
        schema = original.get("prisma/schema.prisma", b"")
        # Accept the exact original demo schema or our own previous conversion.
        normalized = schema.replace(b'provider = "postgresql"', b'provider = "sqlite"')
        if sha(normalized) != BASE_PRISMA:
            raise PatchError("unsupported_prisma_schema")
        if any(name.startswith("prisma/migrations/") for name in original):
            raise PatchError("existing_migrations_unsupported")
        patched["prisma/schema.prisma"] = normalized.replace(b'provider = "sqlite"', b'provider = "postgresql"')
    if plan.storage:
        if plan.storage.path.rstrip("/") != "uploads":
            raise PatchError("unsupported_storage_path")
        adapter = (TEMPLATES / "images.js").read_bytes()
        storage = (TEMPLATES / "storage.js").read_bytes()
        current = original.get("src/images.js", b"")
        if sha(current) != BASE_IMAGES and current != adapter:
            raise PatchError("unsupported_image_callsites")
        if "src/storage.js" in original and original["src/storage.js"] != storage:
            raise PatchError("storage_module_conflict")
        package = json.loads(original.get("package.json", b"{}"))
        if not isinstance(package, dict):
            raise PatchError("unsupported_dependency_graph")
        base = BASE_DEPENDENCIES
        updated = base | {"@aws-sdk/client-s3": SDK_VERSION}
        if (package.get("dependencies") not in (base, updated) or
                package.get("devDependencies") != {"prisma": "6.19.3"} or
                package.get("name") != "demo-app" or package.get("version") != "1.0.0" or
                any(key in package for key in ("overrides", "workspaces", "optionalDependencies",
                                               "peerDependencies", "bundledDependencies"))):
            raise PatchError("unsupported_dependency_graph")
        locked = (TEMPLATES / "package-lock.json").read_bytes()
        current_lock = original.get("package-lock.json")
        if current_lock not in (None, locked, (TEMPLATES / "base-package-lock.json").read_bytes()):
            raise PatchError("unsupported_lockfile")
        patched["src/images.js"] = adapter
        patched["src/storage.js"] = storage
        if package["dependencies"] != updated:
            package["dependencies"] = updated
            patched["package.json"] = (json.dumps(package, ensure_ascii=False, indent=2) + "\n").encode()
        patched["package-lock.json"] = locked
    return patched


def make_diff(original: dict[str, bytes], patched: dict[str, bytes]) -> tuple[str, list[dict]]:
    changes, chunks = [], []
    for name in sorted(original.keys() | patched.keys()):
        before, after = original.get(name), patched.get(name)
        if before == after:
            continue
        if name not in ALLOWED_PATHS or after is None:
            raise PatchError("patch_allowlist_violation")
        changes.append({"path": name, "action": "add" if before is None else "modify",
                        "before_sha256": sha(before) if before is not None else None,
                        "after_sha256": sha(after)})
        # splitlines without endings plus explicit EOF markers keeps a patch
        # applyable even when the public source has no final newline.
        lines = list(difflib.unified_diff(
            (before or b"").decode().splitlines(keepends=True), after.decode().splitlines(keepends=True),
            fromfile=f"a/{name}" if before is not None else "/dev/null", tofile=f"b/{name}"))
        for line in lines:
            chunks.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(chunks), changes


def patch_snapshot(snapshot_dir: Path, repo_map: RepoMap | dict, plan: Plan | dict, output_dir: Path,
                   *, replacements: dict[str, bytes] | None = None) -> dict:
    """Publish a new directory containing source/, patch.diff and manifest.json.

    Caller must supply a frozen snapshot of repo_map.commit. Revision strings
    are checked for consistency, not authenticated against Git by this function.
    A separate Policy Gate must authorize the resulting digest before building.
    """
    try:
        mapping = RepoMap.model_validate(repo_map)
        plan = Plan.model_validate(plan)
        if mapping.commit != plan.source_revision:
            raise PatchError("revision_mismatch")
        root, output = Path(snapshot_dir).absolute(), Path(output_dir).absolute()
        if any(p.is_symlink() for p in (root, *root.parents, output, *output.parents)):
            raise PatchError("symlink_path")
        if output.exists() or output == root or root in output.parents or output in root.parents:
            raise PatchError("output_must_be_new_and_outside_source")
        if not output.parent.is_dir():
            raise PatchError("output_parent_missing")
        original, excluded = read_snapshot(root, mapping)
        if replacements is None:
            patched = transform(original, plan)
        else:
            # Host-owned publication only. Replacements are still untrusted and
            # MUST pass the independent gate before a build can consume them.
            if (not isinstance(replacements, dict) or set(replacements) - ALLOWED_PATHS or
                    any(not isinstance(v, bytes) or len(v) > Limits().max_file_bytes
                        for v in replacements.values())):
                raise PatchError("patch_allowlist_violation")
            patched = original | replacements
            if sum(map(len, patched.values())) > Limits().max_snapshot_bytes:
                raise PatchError("snapshot_too_large")
        diff, changes = make_diff(original, patched)
        if Redactor().contains_secret(diff):
            raise PatchError("secret_in_diff")
        report = {"schema_version": "1.0.0", "profile": "demo-app-v1-v2",
                  "source_revision": mapping.commit, "target": plan.target.value,
                  "status": "patched" if changes else "unchanged",
                  "original_digest": tree_digest(original), "patched_digest": tree_digest(patched),
                  "diff_sha256": sha(diff.encode()), "changes": changes, "excluded_paths": excluded,
                  "requires_policy_gate": True, "data_migration_performed": False}
        with tempfile.TemporaryDirectory(prefix=".code-patch-", dir=output.parent) as temporary:
            stage = Path(temporary) / "result"
            stage.mkdir()
            (stage / "source").mkdir()
            for name, data in patched.items():
                path = stage / "source" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            (stage / "patch.diff").write_text(diff, encoding="utf-8")
            (stage / "manifest.json").write_text(json.dumps(report, indent=2) + "\n")
            # Atomic publication; never replace any existing directory.
            output.mkdir(exist_ok=False)
            try:
                stage.rename(output)
            except BaseException:
                output.rmdir()
                raise
        return report
    except PatchError:
        raise
    except (OSError, ValueError, TypeError):
        raise PatchError("invalid_or_unsupported_patch_input") from None
