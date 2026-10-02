"""Opt-in integration of the pinned C fixture producer with real E Docker runtime.

No AWS operations. --publish deliberately opens a temporary public Quick Tunnel.
The caller owns cleanup; named volumes are never deleted by this script.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from adapters.local.runtime import compose_args, deploy, rollback, smoke
from builder.runtime import RuntimeFailure, build, run
from policy_gate.gate import validate_intent


def verify_recovery(plan, artifact, state, sink):
    previous = json.loads((state / "current.json").read_text())
    injected = False

    def fail_after_start(args, **kwargs):
        nonlocal injected
        output = run(args, **kwargs)
        if "up" in args and not injected:
            injected = True
            raise RuntimeFailure("injected_start_failure")
        return output

    try:
        deploy(
            plan,
            artifact,
            state,
            deployment_id="e-verify-failure",
            sink=sink,
            execute=fail_after_start,
        )
    except RuntimeFailure as exc:
        if str(exc) != "injected_start_failure":
            raise
    else:
        raise RuntimeFailure("failure_was_not_injected")
    restored = json.loads((state / "current.json").read_text())
    if (
        restored["image_id"] != previous["image_id"]
        or restored["config"] != previous["config"]
    ):
        raise RuntimeFailure("automatic_restore_mismatch")
    smoke(restored["url"], record=previous["record"])
    return True


def verify(c_root, output, state, publish=False):
    # C lives in a separate, explicitly chosen trusted developer checkout.
    sys.path.append(str(c_root.resolve()))
    from code_patch.runner import patch_snapshot

    output.mkdir(parents=True, exist_ok=False)
    results = {
        "scope": "B schemas + C fixture bundles + E real Docker",
        "steps": [],
        "public_verified": False,
    }
    events = output / "events.jsonl"

    def sink(event):
        with events.open("a") as stream:
            stream.write(event.jsonl())
        print(event.jsonl(), end="", flush=True)

    states = []
    for version in ("v1", "v2"):
        folder = output / version
        folder.mkdir()
        snapshot = folder / "original"
        fixture = c_root / "tests/fixtures/analyzer" / version
        shutil.copytree(fixture / "snapshot", snapshot)
        shutil.copyfile(
            c_root / "code_patch/templates/base-package-lock.json",
            snapshot / "package-lock.json",
        )
        mapping = json.loads((fixture / "repo_map.json").read_text())
        if "package-lock.json" not in mapping["tree"]:
            mapping["tree"].append("package-lock.json")
        plan = json.loads(
            (ROOT / "schemas/fixtures" / version / "plan.local.json").read_text()
        )
        plan["app"] = state.name
        intent = json.loads(
            (ROOT / "schemas/fixtures" / version / "intent.json").read_text()
        )
        validate_intent(intent, snapshot, plan["source_revision"])
        patch_snapshot(snapshot, mapping, plan, folder / "bundle")
        artifact = build(
            snapshot,
            folder / "bundle",
            plan,
            deployment_id="e-verify-" + version,
            sink=sink,
        )
        (folder / "build.json").write_text(artifact.model_dump_json(indent=2) + "\n")
        result = deploy(
            plan,
            artifact,
            state,
            publish=publish,
            deployment_id="e-verify-" + version,
            sink=sink,
        )
        states.append(result)
        results["steps"].append(
            {
                "version": version,
                "image_id": result["image_id"],
                "revision": plan["source_revision"],
                "health_note_image": True,
                "public_verified": bool(result["public_url"]),
            }
        )
        if version == "v2":
            smoke(result["url"], record=states[0]["record"])
            args = compose_args(state, result["config"])
            worker = run(args + ["ps", "--status", "running", "--services"])
            if "worker" not in worker.splitlines():
                raise RuntimeError("worker_not_running")
            results["worker_running"] = True
            results["public_verified"] = bool(result["public_url"])
    result = rollback(state, deployment_id="e-verify-rollback", sink=sink)
    smoke(result["url"], record=states[1]["record"])
    if result["image_id"] != states[0]["image_id"]:
        raise RuntimeError("wrong_rollback_image")
    results["rollback_and_both_versions_data"] = True
    results["failed_upgrade_automatically_restored"] = verify_recovery(
        plan, artifact, state, sink
    )
    (output / "result.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--c-root", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New directory for reproducible receipts",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        required=True,
        help="Dedicated app state; final component is app name",
    )
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    verify(args.c_root, args.output, args.state_dir.absolute(), args.publish)


if __name__ == "__main__":
    main()
