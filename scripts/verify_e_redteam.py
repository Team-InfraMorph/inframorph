"""Exercise real E gates against the separate redteam corpus; never run its code.

The corpus allowlist is a TEST profile, not Builder's production allowlist.
Prompt/path tool cases remain owned by C and are explicitly reported untested.
"""

import argparse
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


def verify(corpus):
    corpus = Path(corpus).resolve()
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
        if case["category"] not in ("intent", "patch"):
            results.append(
                {"id": case["id"], "status": "not-run", "reason": "requires_C_tools"}
            )
            continue
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
                        allowed_paths=manifest["test_profile"]["allowed_patch_paths"],
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
                    if decision == case["expected"]["decision"]
                    else "fail",
                    "code": code,
                }
            )
    return {
        "profile": "corpus-only",
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
    return bool(result["failed"])


if __name__ == "__main__":
    sys.exit(main())
