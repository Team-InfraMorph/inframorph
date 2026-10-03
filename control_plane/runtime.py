"""D API with C analysis/recovery and actual E Local deployment.

Use --demo explicitly for fixture B; --codex enables fresh local Codex inference.
Use --github for the real Repo Mapper/Planner and --openai for the team API.
AWS requires an explicit operator-owned --aws-config. --publish exposes only the
verified Local app. The control plane stays on localhost.
"""
import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
from typing import Literal

from pydantic import StrictBool
from schemas import Intent, Plan, RepoMap
from schemas.common import ContractModel, Target
from analyzer.backend import OpenAIBackend, ReplayBackend
from analyzer.codex_backend import CodexBackend, LOCAL_MODELS
from analyzer.local_verify import LOCAL_MODEL
from analyzer.config import Limits, MODEL
from analyzer.runner import AnalysisError, analyze
from analyzer.snapshot import Snapshot
from analyzer.redaction import Redactor
from analyzer.recovery import _check_plan
from analyzer.source_policy import SourcePolicyError, validate_demo_intent, validate_demo_plan
from policy_gate.gate import PolicyError, validate_intent, validate_plan
from .policy_results import check as policy_check
from .analysis import AnalysisFailed
from .b_bridge import BCommands, DemoModules, GitHubModules
from .results import metrics as checked_metrics
# Preserve D module command imports while the C Local worker uses an explicit runtime.
from .module_commands import build_cmds, deployer_cmd, fake_deployer_cmd  # noqa: F401


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_PLAN_ERRORS = frozenset({"plan_config_mismatch", "plan_intent_mismatch", "plan_workload_mismatch",
                                "plan_state_mismatch", "plan_secret_mismatch", "plan_resource_unsupported"})


class LocalContext(ContractModel):
    deployment_id: str
    project_id: str
    snapshot: str
    repo_map: RepoMap
    intent: Intent
    plan: Plan
    metrics: dict
    replay: str | None
    state_root: str
    output_dir: str
    fault: str = "none"
    demo: bool = False
    publish: StrictBool = False
    planner_command: list[str] | None = None
    analysis_backend: Literal["replay", "codex-cli", "openai"] = "replay"
    analysis_model: Literal["gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna"] = LOCAL_MODEL
    targets: list[Literal["local", "aws", "onprem"]] = ["local"]
    aws_plan: Plan | None = None


def context_plans(context):
    plans = {"local": context.plan.model_dump(mode="json")}
    # 사내 서버는 Local 테스트와 같은 Docker 구성·같은 이미지. 대상 이름만 onprem
    plans["onprem"] = context.plan.model_copy(update={"target": Target.ONPREM}).model_dump(mode="json")
    if context.aws_plan is not None:
        plans["aws"] = context.aws_plan.model_dump(mode="json")
    return {target: plans[target] for target in context.targets}


def analysis_backend(kind, model, replay):
    if kind == "openai":
        if replay is not None:
            raise ValueError("conflicting_analysis_backends")
        if model != MODEL:
            raise ValueError("unsupported_api_model")
        return OpenAIBackend()
    if kind == "codex-cli":
        if replay is not None:
            raise ValueError("conflicting_analysis_backends")
        return CodexBackend(model)
    if kind != "replay" or replay is None:
        raise ValueError("explicit_analysis_replay_required")
    return ReplayBackend.from_file(Path(replay))


def analysis_limits(kind):
    if kind == "openai":
        return Limits(timeout_seconds=180, max_estimated_usd=0.10)
    return Limits(timeout_seconds=180) if kind == "codex-cli" else Limits()


@asynccontextmanager
async def analysis_session(kind, model, replay):
    backend = analysis_backend(kind, model, replay)
    try:
        yield backend
    finally:
        if kind == "openai":
            await backend.close()


async def analyze_source(kind, model, replay, mapped):
    # Each request owns its client in the same event loop, including failures.
    async with analysis_session(kind, model, replay) as backend:
        return await analyze(mapped.repo_map, mapped.snapshot, backend, analysis_limits(kind),
                             clarify_requirements=requirements_clarifier(mapped.snapshot, mapped.repo_map))


def private_json(path, data):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("runtime_state_symlink")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(data, file)


def record_analysis_diagnostics(path, stats, attempts, *, status, stage, fields=()):
    """Persist host summaries, never model answers or exception/source text.

    Failure to record diagnostics must preserve the original gate outcome. An
    existing/symlinked file is never overwritten, and cannot authorize execution.
    """
    from analyzer.source_policy import POLICY_FIELDS
    try:
        private_json(path, {"schema_version": 1, "status": status, "stage": stage,
            "policy_fields": [name for name in POLICY_FIELDS if name in fields],
            "metrics": checked_metrics(stats), "attempts": attempts})
        return True
    except (OSError, ValueError):
        return False


def requirements_clarifier(source, mapping):
    def clarify(candidate):
        try:
            validate_demo_intent(candidate, source, mapping)
        except SourcePolicyError as error:
            return error.code == "intent_source_mismatch" and error.fields == ("unknowns",)
        except Exception:
            return False  # The final policy reports the failure.
        return False
    return clarify


class LocalRuntime:
    def __init__(self, *, root, b_modules, replay=None, fault="none", publish=False,
                 codex=False, openai=False, model=LOCAL_MODEL, aws_config=None, onprem_config=None):
        self.root = Path(root).absolute()
        if any(p.is_symlink() for p in (self.root, *self.root.parents)):
            raise ValueError("runtime_state_symlink")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if type(publish) is not bool:
            raise ValueError("invalid_publish_option")
        self.publish = publish
        self.b = b_modules
        self.mapper_mode = getattr(b_modules, "source_mode", "external")
        self.aws_config = Path(aws_config).absolute() if aws_config else None
        if self.aws_config:
            from .aws_config import load_config
            load_config(self.aws_config)
        self.onprem_config = Path(onprem_config).absolute() if onprem_config else None
        if self.onprem_config:
            from .onprem_config import load_config as load_onprem
            load_onprem(self.onprem_config)
        self.replay = replay
        if sum((bool(codex), bool(openai), replay is not None)) > 1:
            raise ValueError("conflicting_analysis_backends")
        if openai and model != MODEL:
            raise ValueError("unsupported_api_model")
        if openai and not os.environ.get("OPENAI_API_KEY"):
            raise ValueError("missing_openai_api_key")
        if model not in LOCAL_MODELS:
            raise ValueError("unsupported_local_model")
        self.analysis_backend = "openai" if openai else "codex-cli" if codex else "replay"
        self.analysis_model = model
        self.fault = fault
        if fault not in {"none", "first", "always"} or fault != "none" and not isinstance(b_modules, DemoModules):
            raise ValueError("fault_injection_requires_demo")
        if not isinstance(b_modules, DemoModules) and replay is None and not (codex or openai):
            raise ValueError("explicit_analysis_replay_required")

    def demo_versions(self):
        return self.b.versions() if isinstance(self.b, (DemoModules, GitHubModules)) else []

    def demo_revision(self, project, version):
        if project["repo_url"].rstrip("/").removesuffix(".git") != "https://github.com/Team-InfraMorph/demo-app":
            raise ValueError("demo_repository_required")
        for item in self.demo_versions():
            if item["id"] == version:
                return item["commit_sha"]
        raise ValueError("demo_version_unavailable")

    def context_file(self, deployment_id):
        if not __import__("re").fullmatch(r"[A-Za-z0-9-]{1,100}", deployment_id):
            raise ValueError("invalid_deployment_id")
        return self.root / "deployments" / deployment_id / "context.json"

    def analyze(self, store, deployment):
        targets = set(deployment["targets"])
        if (not targets or not targets <= {"local", "aws", "onprem"} or ("aws" in targets and self.aws_config is None)
                or ("onprem" in targets and (self.onprem_config is None or "local" not in targets))):
            raise AnalysisFailed("local_runtime_only")
        folder = self.context_file(deployment["id"]).parent
        project = store.get_project(deployment["project_id"])
        stats, stage = {}, "mapper"
        attempts, report_fields = [], ()
        folder_created, report_status = False, "failed"
        try:
            folder.mkdir(parents=True, exist_ok=False, mode=0o700)
            folder_created = True
            mapped = self.b.map(project, deployment, folder)
            stage = "snapshot"
            replay = (None if self.analysis_backend != "replay" else
                      Path(self.replay or self.b.replay(mapped.repo_map)).absolute())
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
                stats = {"backend": self.analysis_backend, "model": self.analysis_model,
                         "model_calls": 0, "api_calls": 0,
                         "snapshot_digest": snapshot.digest, "usage_complete": True}
            else:
                stage = "analyzer"
                result = asyncio.run(analyze_source(self.analysis_backend, self.analysis_model, replay, mapped))
                intent, stats = result.intent, asdict(result.metrics)
                attempts = result.diagnostics
            stage = "intent_policy"
            for target in sorted(targets):
                policy_check(store, deployment["id"], target, "source", lambda: validate_demo_intent(intent, mapped.snapshot, mapped.repo_map))
            stage = "intent_gate"
            for target in sorted(targets):
                policy_check(store, deployment["id"], target, "intent", lambda: validate_intent(intent, mapped.snapshot, mapped.repo_map.commit))
            stage = "planner"
            if deployment.get("triggered_by") == "rollback" and "local" in targets:
                plan = Plan.model_validate(store.get_plans(deployment["id"])["local"])
            else:
                plan = self.b.plan(intent)
            stage = "plan_policy"
            _check_plan(intent, plan, mapped.repo_map.commit)
            if "local" in targets:
                policy_check(store, deployment["id"], "local", "plan", lambda: (validate_demo_plan(plan, mapped.repo_map), validate_plan(intent, plan)))
            aws_plan = None
            if "aws" in targets:
                aws_plan = (Plan.model_validate(store.get_plans(deployment["id"])["aws"])
                            if deployment.get("triggered_by") == "rollback" else self.b.plan(intent, "aws"))
                policy_check(store, deployment["id"], "aws", "plan", lambda: (validate_demo_plan(aws_plan, mapped.repo_map, target="aws"), validate_plan(intent, aws_plan)))
            stage = "context"
            context = LocalContext(deployment_id=deployment["id"], project_id=project["project_id"],
                snapshot=str(mapped.snapshot), repo_map=mapped.repo_map, intent=intent, plan=plan,
                metrics=stats, replay=str(replay) if replay else None, state_root=str(self.root / "projects"),
                output_dir=str(folder), fault=self.fault, demo=isinstance(self.b, DemoModules), publish=self.publish,
                planner_command=None if isinstance(self.b, DemoModules) else self.b.planner_command,
                analysis_backend=self.analysis_backend, analysis_model=self.analysis_model,
                targets=list(deployment["targets"]), aws_plan=aws_plan)
            private_json(folder / "context.json", context.model_dump(mode="json"))
            report_status = "passed"
            return {"commit_sha": mapped.repo_map.commit, "repo_map": mapped.repo_map.model_dump(mode="json"),
                    "intent": intent.model_dump(mode="json"), "plans": context_plans(context), "metrics": stats, "intent_checked": True}
        except AnalysisError as error:
            stats, attempts = asdict(error.metrics), error.diagnostics
            raise AnalysisFailed(error.code, stats | {"blocked_stage": stage}) from None
        except SourcePolicyError as error:
            report_fields = error.fields
            raise AnalysisFailed(error.code, stats | {"blocked_stage": stage,
                                 "policy_fields": list(error.fields)},
                                 step="plan" if stage == "plan_policy" else "analyze") from None
        except PolicyError as error:
            # Only reviewed fixed codes may cross the public API boundary.
            code = str(error) if str(error) in PUBLIC_PLAN_ERRORS else "policy_gate_failed"
            raise AnalysisFailed(code, stats | {"blocked_stage": stage},
                                 step="plan" if stage == "plan_policy" else "policy") from None
        except Exception:
            # A downstream gate/Planner failure must not discard consumed usage
            # or publish the rejected Intent/Plan. Stage names are host constants.
            raise AnalysisFailed("local_pipeline_analysis_failed", stats | {"blocked_stage": stage},
                                 step="plan" if stage in {"planner", "plan_policy"} else "analyze") from None
        finally:
            if folder_created:
                record_analysis_diagnostics(folder / "analysis-diagnostics.json", stats, attempts,
                    status=report_status, stage=stage, fields=report_fields)

    def command(self, store, deployment_id, target):
        if (target not in {"local", "aws", "onprem"} or (target == "aws" and self.aws_config is None)
                or (target == "onprem" and self.onprem_config is None)):
            raise ValueError("local_runtime_only")
        path = self.context_file(deployment_id)
        if not path.exists():
            # D's rollback path skips ready(); use the explicitly authorized old
            # revision/Plan and cached Intent without a new model inference.
            deployment = store.get_deployment(deployment_id)
            result = self.analyze(store, deployment)
            store.set_analysis_metrics(deployment_id, result["metrics"])
            store.save_initial_analysis(deployment_id, result)
        command = [sys.executable, "-m", f"control_plane.{target}_deploy", "--context", str(path),
                   "--database", str(store.path.absolute())]
        if target == "local" and self.analysis_backend == "openai":
            command.append("--openai")  # Authorize only this trusted worker's recovery API credential.
        if target == "aws":
            command += ["--aws-config", str(self.aws_config)]
        if target == "onprem":
            command += ["--onprem-config", str(self.onprem_config)]
        return command


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mapping = parser.add_mutually_exclusive_group()
    mapping.add_argument("--demo", action="store_true", help="Explicit fixture Mapper/Planner and stored model responses")
    mapping.add_argument("--github", action="store_true", help="Fetch GitHub source with Repo Mapper and use the real B Planner")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, help="Server .env path; defaults to this checkout's .env")
    parser.add_argument("--mapper-command", help="Operator-owned argv as a JSON list; request arrives on stdin")
    parser.add_argument("--planner-command", help="Operator-owned argv as a JSON list; request arrives on stdin")
    backend = parser.add_mutually_exclusive_group()
    backend.add_argument("--replay", type=Path)
    backend.add_argument("--codex", action="store_true", help="Fresh ChatGPT-authenticated local Codex analysis")
    backend.add_argument("--openai", action="store_true", help="Real gpt-6-luna/low API analysis; requires OPENAI_API_KEY")
    parser.add_argument("--model", choices=LOCAL_MODELS, default=LOCAL_MODEL)
    parser.add_argument("--publish", action="store_true", help="Publish only the Local app via cloudflared; never the control plane")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--aws-config", type=Path, help="Private operator-owned AWS configuration; never repository/model input")
    parser.add_argument("--onprem-config", type=Path, help="Private operator-owned on-prem Docker host (SSH); never repository/model input")
    args = parser.parse_args(argv)
    from .environment import load_server_environment
    load_server_environment(args.env_file)
    if not 1 <= args.port <= 65535:
        parser.error("invalid port")
    if (args.demo or args.github) and (args.mapper_command or args.planner_command):
        parser.error("Select built-in modules or explicit B commands, not both")
    if not args.demo and (not (args.github or args.mapper_command and args.planner_command) or
                          not (args.replay or args.codex or args.openai)):
        parser.error("--github or B commands and an explicit analysis backend are required")
    if args.openai and (args.model != MODEL or not os.environ.get("OPENAI_API_KEY")):
        parser.error("--openai requires gpt-6-luna and OPENAI_API_KEY")
    modules = (DemoModules() if args.demo else GitHubModules() if args.github else
               BCommands(mapper_command=json.loads(args.mapper_command), planner_command=json.loads(args.planner_command)))
    runtime = LocalRuntime(root=args.root / "runtime", b_modules=modules, replay=args.replay, publish=args.publish,
                           codex=args.codex, openai=args.openai, model=args.model, aws_config=args.aws_config,
                           onprem_config=args.onprem_config)
    from .app import create_app
    import uvicorn
    app = create_app(db_path=args.root / "control-plane.db", runtime=runtime)
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
