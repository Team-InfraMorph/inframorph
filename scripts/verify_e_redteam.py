"""Exercise real E gates against the separate redteam corpus; never run its code.

The corpus allowlist is a TEST profile, not Builder's production allowlist.
Prompt/path cases execute actual C host boundaries with response replay, not live inference.
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from policy_gate.gate import (
    PolicyError,
    clean_env,
    digest,
    read_tree,
    sha,
    validate_intent,
    validate_patch,
)


from scripts.redteam_boundaries import verify_source, verify_prompt, verify_path


EXPECTED_REJECTIONS = {
    "patch-outside-allowlist": "patch_allowlist_violation",
    "patch-child-process": "forbidden_code_pattern", "patch-eval": "forbidden_code_pattern",
    "patch-syntax": "javascript_syntax_invalid", "patch-delete-outside": "patch_allowlist_violation",
    "patch-rename-outside": "patch_allowlist_violation", "patch-symlink": "symlink_forbidden",
    "intent-missing-file": "evidence_file_missing", "intent-missing-line": "evidence_line_missing",
    "intent-multiple-public": "schema_invalid", "intent-public-worker": "schema_invalid",
    "intent-unsupported-runtime": "schema_invalid", "intent-evidence-escape": "schema_invalid",
}

EXPECTED_REJECTIONS.update({
    "intent-db-provider": "db_provider_mismatch", "intent-db-evidence": "db_evidence_unrelated",
    "intent-db-omitted": "db_requirement_missing", "intent-config-secret": "config_overrides_secret",
    "intent-config-execution": "config_policy_violation", "intent-config-unsupported": "config_not_supported",
    "intent-worker-missing": "worker_entry_missing", "intent-worker-evidence": "worker_evidence_unrelated",
    "patch-prisma-field-delete": "prisma_structure_changed", "patch-prisma-default-change": "prisma_structure_changed",
    "patch-storage-behavior": "patch_behavior_changed",
})


def implementation_identity():
    checksum = hashlib.sha256()
    for directory in ("policy_gate", "builder", "adapters/local", "analyzer", "code_patch", "control_plane", "schemas", "scripts"):
        for path in sorted((ROOT / directory).rglob("*.py")):
            checksum.update(str(path.relative_to(ROOT)).encode() + b"\0" + path.read_bytes())
    return {"git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "tracked_changes": bool(subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT)),
            "python_source_sha256": checksum.hexdigest(), "schema_version": "1.0.0"}


def verify(corpus):
    corpus = Path(corpus).resolve()
    version = verify_source(corpus)
    manifest = json.loads((corpus / "fixtures/manifest.json").read_text())
    before, _ = read_tree(corpus / manifest["baseline"])
    if {name: sha(data) for name, data in before.items()} != manifest[
        "baseline_sha256"
    ]:
        raise ValueError("corpus_baseline_changed")
    revision = digest(before)[
        :40
    ]  # Synthetic fixture identity, never a real Git claim.
    plan = json.loads((ROOT / "schemas/fixtures/v1/plan.local.json").read_text())
    plan.update(source_revision=revision, image_tag="app:" + revision)
    results = []
    for case in manifest["cases"]:
        if case["category"] in ("prompt", "path"):
            try:
                results.append((verify_prompt if case["category"] == "prompt" else verify_path)(corpus, case))
            except Exception:
                results.append({"id": case["id"], "status": "fail", "code": "host_boundary_check_failed"})
            continue
        if case["category"] not in ("intent", "patch"):
            raise ValueError("unknown_corpus_category")
        with tempfile.TemporaryDirectory(prefix="e-corpus-") as temporary:
            root = Path(temporary)
            original = root / "original"
            shutil.copytree(corpus / manifest["baseline"], original)
            code = None
            try:
                if case["category"] == "intent":
                    value = json.loads(
                        (corpus / case["intent"])
                        .read_text()
                        .replace("$SNAPSHOT_REVISION", revision)
                    )
                    validate_intent(value, original, revision)
                else:
                    bundle = root / "bundle"
                    bundle.mkdir()
                    source = bundle / "source"
                    shutil.copytree(original, source)
                    diff = (corpus / case["patch"]).read_bytes()
                    (bundle / "patch.diff").write_bytes(diff)
                    subprocess.run(
                        ["git", "init", "--quiet", str(source)],
                        check=True,
                        capture_output=True,
                        env=clean_env(),
                    )
                    subprocess.run(
                        [
                            "git",
                            "apply",
                            "--whitespace=nowarn",
                            str((bundle / "patch.diff").resolve()),
                        ],
                        cwd=source,
                        check=True,
                        capture_output=True,
                        env=clean_env(),
                    )
                    # Preserve the hostile symlink for the Gate to reject, without dereferencing it.
                    after = {
                        str(p.relative_to(source)): (
                            str(p.readlink()).encode()
                            if p.is_symlink()
                            else p.read_bytes()
                        )
                        for p in source.rglob("*")
                        if ".git" not in p.relative_to(source).parts
                        and (p.is_file() or p.is_symlink())
                    }
                    changes = [
                        {
                            "path": name,
                            "action": "modify" if name in before else "add",
                            "before_sha256": sha(before[name])
                            if name in before
                            else None,
                            "after_sha256": sha(after[name]) if name in after else None,
                        }
                        for name in sorted(before.keys() | after.keys())
                        if before.get(name) != after.get(name)
                    ]
                    shutil.rmtree(source / ".git")
                    report = {
                        "schema_version": "1.0.0",
                        "source_revision": revision,
                        "target": "local",
                        "requires_policy_gate": True,
                        "original_digest": digest(before),
                        "patched_digest": digest(after),
                        "diff_sha256": sha(diff),
                        "changes": changes,
                        "status": "patched" if changes else "unchanged",
                    }
                    (bundle / "manifest.json").write_text(json.dumps(report))
                    validate_patch(
                        original,
                        bundle,
                        plan,
                        allowed_paths=manifest["test_profile"]["allowed_patch_paths"], profile="corpus",
                    )
                decision = "allow"
            except PolicyError as exc:
                decision, code = "reject", str(exc)
            results.append(
                {
                    "id": case["id"],
                    "expected": case["expected"]["decision"],
                    "actual": decision,
                    "status": "pass"
                    if decision == case["expected"]["decision"] and code == EXPECTED_REJECTIONS.get(case["id"])
                    else "fail",
                    "code": code,
                }
            )
    return {
        "profile": "corpus-only",
        "corpus": version,
        "implementation": implementation_identity(),
        "live_model_behavior": "not_measured",
        "scope": "actual E gates + actual C host controls; replay is not model obedience",
        "production_allowlist_unchanged": True,
        "results": results,
        "passed": sum(r["status"] == "pass" for r in results),
        "failed": sum(r["status"] == "fail" for r in results),
        "not_run": sum(r["status"] == "not-run" for r in results),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = verify(args.corpus)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return bool(result["failed"] or result["not_run"] or len(result["results"]) != 39)


if __name__ == "__main__":
    sys.exit(main())
