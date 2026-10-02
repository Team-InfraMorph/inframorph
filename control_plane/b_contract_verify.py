"""Check B command contracts through real C/E gates, without build/deploy/API calls."""
import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import sys
import uuid

from analyzer.backend import ReplayBackend
from analyzer.config import Limits
from analyzer.redaction import Redactor
from analyzer.recovery import PatchedCandidate, _assert_patch, _check_plan, fingerprint
from analyzer.runner import analyze
from analyzer.snapshot import Snapshot
from analyzer.source_policy import validate_demo_intent, validate_demo_plan
from code_patch import patch_snapshot
from policy_gate.gate import validate_intent, validate_patch
from .b_bridge import BCommandError, BCommands, ROOT
from .runtime import private_json


def fixture_commands():
    return {name: [sys.executable, "-m", "control_plane.b_contract_fixture", name]
            for name in ("mapper", "planner")}


def verify_case(modules, case, folder, *, repo_url, branch):
    folder.mkdir(mode=0o700)
    fixture = ROOT / "tests/fixtures/analyzer" / case
    revision = json.loads((fixture / "repo_map.json").read_text())["commit"]
    report = {"case": case, "expected_revision": revision, "status": "failed", "checks": []}
    stage = "mapper"
    try:
        mapped = modules.map({"repo_url": repo_url, "branch": branch}, {"commit_sha": revision}, folder)
        snapshot = Snapshot(mapped.snapshot, mapped.repo_map.tree, Limits(), Redactor())
        report["snapshot_digest"] = snapshot.digest
        report["checks"].append("mapper_exact_revision_and_readable_snapshot")
        stage = "analyzer"
        result = asyncio.run(analyze(mapped.repo_map, mapped.snapshot, ReplayBackend.from_file(fixture / "replay.json")))
        report["metrics"] = asdict(result.metrics)
        report["checks"].append("c_read_grep_glob_with_stored_responses")
        stage = "intent_gate"
        validate_demo_intent(result.intent, mapped.snapshot, mapped.repo_map)
        validate_intent(result.intent, mapped.snapshot, revision)
        report["checks"].append("source_policy_and_real_e_intent_gate")
        stage = "planner"
        plan = modules.plan(result.intent)
        _check_plan(result.intent, plan, revision)
        validate_demo_plan(plan, mapped.repo_map)
        report["checks"].append("planner_local_revision_and_execution_policy")
        stage = "patch"
        bundle = folder / "patch"
        manifest = patch_snapshot(mapped.snapshot, mapped.repo_map, plan, bundle)
        files = tuple(sorted(set(mapped.repo_map.tree) | {c["path"] for c in manifest["changes"]}))
        candidate = PatchedCandidate(bundle, manifest, plan, files)
        _assert_patch(candidate, mapped.repo_map)
        report["checks"].append("c_patch_manifest_and_copy_binding")
        stage = "patch_gate"
        validate_patch(mapped.snapshot, bundle, plan)
        report["checks"].append("real_e_patch_gate")
        # Store only validated contracts, never raw B command output or stderr.
        private_json(folder / "intent.json", result.intent.model_dump(mode="json"))
        private_json(folder / "plan.json", plan.model_dump(mode="json"))
        report.update(status="passed", intent_fingerprint=fingerprint(result.intent),
            plan_fingerprint=fingerprint(plan), patch_fingerprint=candidate.fingerprint,
            patched_digest=manifest["patched_digest"], diff_sha256=manifest["diff_sha256"])
    except Exception as error:
        report.update(stage=stage, error=str(error) if isinstance(error, BCommandError) else "b_contract_rejected")
    private_json(folder / "report.json", report)
    return report


def verify(modules, output, *, cases=("v1", "v2"), fixture_mode=False,
           repo_url="https://github.com/Team-InfraMorph/demo-app", branch="main"):
    output = Path(output).absolute()
    if any(p.is_symlink() for p in (output, *output.parents)):
        raise ValueError("contract_output_symlink")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    reports = [verify_case(modules, case, output / case, repo_url=repo_url, branch=branch) for case in cases]
    summary = {"status": "passed" if all(r["status"] == "passed" for r in reports) else "failed",
        "b_commands": "fixture command stand-ins" if fixture_mode else "operator-provided commands",
        "analysis": "real C tool loop with stored response replay", "results": reports,
        "team_api_called": False, "docker_called": False, "aws_called": False, "publish": False,
        "scope": "reviewed demo-app v1/v2 command/schema/source/patch contract; not general B semantics or remote Git authenticity"}
    private_json(output / "summary.json", summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-commands", action="store_true", help="Opt in to command stand-ins; does not test actual B implementation")
    parser.add_argument("--mapper-command", help="Operator-owned argv encoded as JSON")
    parser.add_argument("--planner-command", help="Operator-owned argv encoded as JSON")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--case", choices=("v1", "v2", "all"), default="all")
    parser.add_argument("--repo-url", default="https://github.com/Team-InfraMorph/demo-app")
    parser.add_argument("--branch", default="main")
    args = parser.parse_args(argv)
    if args.fixture_commands and (args.mapper_command or args.planner_command):
        parser.error("fixture commands and actual B commands are separate modes")
    if not args.fixture_commands and (not args.mapper_command or not args.planner_command):
        parser.error("both B commands are required; no implicit fixture fallback")
    try:
        commands = fixture_commands() if args.fixture_commands else {
            "mapper": json.loads(args.mapper_command), "planner": json.loads(args.planner_command)}
        modules = BCommands(mapper_command=commands["mapper"], planner_command=commands["planner"])
    except ValueError:
        parser.error("commands must be nonempty JSON argv arrays")
    output = args.output_dir or ROOT / ".local/b-contract-verification" / uuid.uuid4().hex[:12]
    summary = verify(modules, output, cases=("v1", "v2") if args.case == "all" else (args.case,),
        fixture_mode=args.fixture_commands, repo_url=args.repo_url, branch=args.branch)
    print(json.dumps({"status": summary["status"], "b_commands": summary["b_commands"],
        "cases": [{"case": r["case"], "status": r["status"], "stage": r.get("stage"), "error": r.get("error")}
                  for r in summary["results"]], "summary": str(output / "summary.json")}))
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
