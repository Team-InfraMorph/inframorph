"""D API with C analysis/recovery and actual E Local deployment.

Use --demo explicitly for fixture B and response replay. This entry point does
not call the team model API or AWS. --publish exposes only the verified Local app.
"""
import argparse
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys

from pydantic import StrictBool
from schemas import Intent, Plan, RepoMap
from schemas.common import ContractModel
from analyzer.backend import ReplayBackend
from analyzer.config import Limits
from analyzer.runner import AnalysisError, analyze
from analyzer.snapshot import Snapshot
from analyzer.redaction import Redactor
from analyzer.recovery import _check_plan
from analyzer.source_policy import validate_demo_intent, validate_demo_plan
from policy_gate.gate import validate_intent
from .analysis import AnalysisFailed
from .b_bridge import BCommands, DemoModules
# Preserve D module command imports while the C Local worker uses an explicit runtime.
from .module_commands import build_cmds, deployer_cmd, fake_deployer_cmd  # noqa: F401


ROOT = Path(__file__).resolve().parents[1]


class LocalContext(ContractModel):
    deployment_id: str
    project_id: str
    snapshot: str
    repo_map: RepoMap
    intent: Intent
    plan: Plan
    metrics: dict
    replay: str
    state_root: str
    output_dir: str
    fault: str = "none"
    demo: bool = False
    publish: StrictBool = False
    planner_command: list[str] | None = None


def private_json(path, data):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("runtime_state_symlink")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(data, file)


class LocalRuntime:
    def __init__(self, *, root, b_modules, replay=None, fault="none", publish=False):
        self.root = Path(root).absolute()
        if any(p.is_symlink() for p in (self.root, *self.root.parents)):
            raise ValueError("runtime_state_symlink")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if type(publish) is not bool:
            raise ValueError("invalid_publish_option")
        self.publish = publish
        self.b = b_modules
        self.replay = replay
        self.fault = fault
        if fault not in {"none", "first", "always"} or fault != "none" and not isinstance(b_modules, DemoModules):
            raise ValueError("fault_injection_requires_demo")
        if not isinstance(b_modules, DemoModules) and replay is None:
            raise ValueError("explicit_analysis_replay_required")

    def context_file(self, deployment_id):
        if not __import__("re").fullmatch(r"[A-Za-z0-9-]{1,100}", deployment_id):
            raise ValueError("invalid_deployment_id")
        return self.root / "deployments" / deployment_id / "context.json"

    def analyze(self, store, deployment):
        if set(deployment["targets"]) != {"local"}:
            raise AnalysisFailed("local_runtime_only")
        folder = self.context_file(deployment["id"]).parent
        project = store.get_project(deployment["project_id"])
        stats, stage = {}, "mapper"
        try:
            folder.mkdir(parents=True, exist_ok=False, mode=0o700)
            mapped = self.b.map(project, deployment, folder)
            stage = "snapshot"
            replay = Path(self.replay or self.b.replay(mapped.repo_map)).absolute()
            snapshot = Snapshot(mapped.snapshot, mapped.repo_map.tree, Limits(), Redactor())
            if deployment.get("analysis_mode") == "rebuild_only" or deployment.get("triggered_by") == "rollback":
                stage = "cached_intent"
                cached = store.get_analysis(project["project_id"], mapped.repo_map.commit)
                if cached is None and deployment.get("analysis_mode") == "rebuild_only":
                    base = store.last_live(project["project_id"], deployment["id"])
                    cached = store.get_analysis(project["project_id"], base["commit_sha"]) if base else None
                if cached is None or cached["intent"] is None:
                    raise ValueError("cached_intent_unavailable")
                intent = Intent.model_validate(cached["intent"] | {"source_revision": mapped.repo_map.commit})
                stats = {"backend": "replay", "model_calls": 0, "api_calls": 0,
                         "snapshot_digest": snapshot.digest, "usage_complete": True}
            else:
                stage = "analyzer"
                result = asyncio.run(analyze(mapped.repo_map, mapped.snapshot, ReplayBackend.from_file(replay)))
                intent, stats = result.intent, asdict(result.metrics)
            stage = "intent_policy"
            validate_demo_intent(intent, mapped.snapshot, mapped.repo_map)
            stage = "intent_gate"
            validate_intent(intent, mapped.snapshot, mapped.repo_map.commit)
            stage = "planner"
            if deployment.get("triggered_by") == "rollback":
                plan = Plan.model_validate(store.get_plans(deployment["id"])["local"])
            else:
                plan = self.b.plan(intent)
            stage = "plan_policy"
            _check_plan(intent, plan, mapped.repo_map.commit)
            validate_demo_plan(plan, mapped.repo_map)
            stage = "context"
            context = LocalContext(deployment_id=deployment["id"], project_id=project["project_id"],
                snapshot=str(mapped.snapshot), repo_map=mapped.repo_map, intent=intent, plan=plan,
                metrics=stats, replay=str(replay), state_root=str(self.root / "projects"),
                output_dir=str(folder), fault=self.fault, demo=isinstance(self.b, DemoModules), publish=self.publish,
                planner_command=None if isinstance(self.b, DemoModules) else self.b.planner_command)
            private_json(folder / "context.json", context.model_dump(mode="json"))
            return {"commit_sha": mapped.repo_map.commit, "repo_map": mapped.repo_map.model_dump(mode="json"),
                    "intent": intent.model_dump(mode="json"), "plans": {"local": plan.model_dump(mode="json")}, "metrics": stats, "intent_checked": True}
        except AnalysisError as error:
            raise AnalysisFailed(error.code, asdict(error.metrics) | {"blocked_stage": stage}) from None
        except Exception:
            # A downstream gate/Planner failure must not discard consumed usage
            # or publish the rejected Intent/Plan. Stage names are host constants.
            raise AnalysisFailed("local_pipeline_analysis_failed", stats | {"blocked_stage": stage}) from None

    def command(self, store, deployment_id, target):
        if target != "local":
            raise ValueError("local_runtime_only")
        path = self.context_file(deployment_id)
        if not path.exists():
            # D's rollback path skips ready(); use the explicitly authorized old
            # revision/Plan and cached Intent without a new model inference.
            deployment = store.get_deployment(deployment_id)
            result = self.analyze(store, deployment)
            store.set_analysis_metrics(deployment_id, result["metrics"])
            store.save_initial_analysis(deployment_id, result)
        return [sys.executable, "-m", "control_plane.local_deploy", "--context", str(path),
                "--database", str(store.path.absolute())]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="Explicit fixture Mapper/Planner and stored model responses")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--mapper-command", help="Operator-owned argv as a JSON list; request arrives on stdin")
    parser.add_argument("--planner-command", help="Operator-owned argv as a JSON list; request arrives on stdin")
    parser.add_argument("--replay", type=Path)
    parser.add_argument("--publish", action="store_true", help="Publish only the Local app via cloudflared; never the control plane")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("invalid port")
    if not args.demo and (not args.mapper_command or not args.planner_command or not args.replay):
        parser.error("B commands and an explicit replay are required; --demo is a separate opt-in mode")
    modules = DemoModules() if args.demo else BCommands(mapper_command=json.loads(args.mapper_command),
                                                       planner_command=json.loads(args.planner_command))
    runtime = LocalRuntime(root=args.root / "runtime", b_modules=modules, replay=args.replay, publish=args.publish)
    from .app import create_app
    import uvicorn
    app = create_app(db_path=args.root / "control-plane.db", runtime=runtime)
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
