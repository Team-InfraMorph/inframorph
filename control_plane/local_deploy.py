"""Private D deployment process: real E runtime, bounded C recovery, JSONL events."""
import argparse
import asyncio
from dataclasses import asdict, fields
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import sys

from schemas import DeployEvent, Plan
from analyzer.e_runtime import EConnector, EWorkerError, run_worker
from analyzer.feedback import PatchReference
from analyzer.recovery import Approval, PatchedCandidate, _assert_patch, _check_plan, fingerprint, recover_local
from analyzer.retry_store import RetryStore
from analyzer.runner import Metrics
from analyzer.snapshot import Snapshot
from analyzer.config import Limits, MODEL
from analyzer.redaction import Redactor
from code_patch import patch_snapshot
from .b_bridge import DemoModules, call_json
from .change_detector import plan_diff
from .db import Store
from .results import finish_run
from .runtime import (LocalContext, analysis_session, analysis_limits, context_plans,
                      record_analysis_diagnostics, requirements_clarifier)


def load_context(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("runtime_context_symlink")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as file:
        raw = file.read(120_001)
    if len(raw) > 120_000:
        raise ValueError("runtime_context_limit")
    context = LocalContext.model_validate_json(raw)
    folder = path.parent
    root = folder.parent.parent
    if (folder.name != context.deployment_id or folder.parent.name != "deployments" or
            Path(context.output_dir) != folder or Path(context.snapshot) != folder / "snapshot" or
            Path(context.state_root) != root / "projects" or
            not __import__("re").fullmatch(r"p-[0-9a-f]{12}", context.project_id) or
            context.fault not in {"none", "first", "always"} or
            (context.fault != "none" and not context.demo) or
            (not context.demo and not context.planner_command) or
            (context.analysis_backend in {"codex-cli", "openai"} and (context.replay is not None or
                context.metrics.get("backend") != context.analysis_backend or
                context.metrics.get("model") != context.analysis_model)) or
            (context.analysis_backend == "openai" and context.analysis_model != MODEL) or
            (context.analysis_backend == "replay" and context.replay is None)):
        raise ValueError("runtime_context_binding_mismatch")
    _check_plan(context.intent, context.plan, context.repo_map.commit)
    if not context.targets or len(set(context.targets)) != len(context.targets):
        raise ValueError("runtime_context_binding_mismatch")
    if ("aws" in context.targets) != (context.aws_plan is not None):
        raise ValueError("runtime_context_binding_mismatch")
    if context.aws_plan is not None:
        from analyzer.source_policy import validate_demo_plan
        validate_demo_plan(context.aws_plan, context.repo_map, target="aws")
    return context


def emit(context, step, status, code, url=None):
    event = DeployEvent(deployment_id=context.deployment_id, ts=datetime.now(timezone.utc),
        target="local", step=step, status=status, detail=json.dumps({"code": code}), url=url)
    send(event)


def send(event):
    sys.stdout.write(event.jsonl())
    sys.stdout.flush()


def public_failure_code(stage, error):
    if isinstance(error, EWorkerError):
        return str(error)
    # E exposes fixed codes. Never publish arbitrary worker/source error text.
    build_codes = {
        "image_build_wait_timeout", "image_build_already_running",
        "image_tag_collision", "image_platform_mismatch",
        "image_provenance_mismatch", "untrusted_build_lock",
        "command_failed", "command_unavailable_or_timeout",
    }
    if stage == "build" and isinstance(error, (ValueError, RuntimeError)) and str(error) in build_codes:
        return str(error)
    return "local_pipeline_failed"


async def deploy(context, store):
    initial = store.get_deployment_analysis(context.deployment_id)
    deployment = store.get_deployment(context.deployment_id)
    if (deployment is None or deployment["project_id"] != context.project_id or initial is None or
            initial["initial"]["intent"] != context.intent.model_dump(mode="json") or
            initial["initial"]["repo_map"] != context.repo_map.model_dump(mode="json") or
            "local" not in context.targets or
            initial["initial"]["plans"] != context_plans(context)):
        raise ValueError("runtime_context_binding_mismatch")
    store.claim_runtime_run(context.deployment_id, context.repo_map.commit)

    async def make_plan(intent):
        plan = (DemoModules().plan(intent) if context.demo else
                Plan.model_validate(await asyncio.to_thread(call_json, context.planner_command,
                    {"intent": intent.model_dump(mode="json"), "target": "local"})))
        from .policy_results import check
        from policy_gate.gate import validate_plan
        check(store,context.deployment_id,'local','plan',lambda:validate_plan(intent,plan),attempt=1)
        _check_plan(intent, plan, context.repo_map.commit)
        if plan_diff({"local": context.plan.model_dump(mode="json")}, {"local": plan.model_dump(mode="json")}):
            # A recovery cannot approve a new infrastructure shape on behalf of D.
            raise ValueError("recovery_requires_approval")
        return plan

    async def fault_worker(payload):
        return await run_worker(payload | {"fault": context.fault, "fault_dir": str(Path(context.output_dir) / "fault")},
                                module="control_plane.runtime_fault_worker")

    from .policy_results import save as save_policy
    from .policy_lifecycle import guard, receipt
    connector = EConnector(snapshot=context.snapshot, repo_map=context.repo_map,
        state_root=context.state_root, runtime_name="cp-" + context.project_id,
        deployment_id=context.deployment_id, make_plan=make_plan,
        worker=fault_worker if context.fault != "none" else None, publish=context.publish,
        policy_database=str(store.path.absolute()),
        policy_guard=lambda: guard(store,context.deployment_id),
        receipt_sink=lambda value: receipt(store,context.deployment_id,"local",value),
        policy_sink=lambda attempt, report: save_policy(store, context.deployment_id, "local", attempt, report))
    stage = "policy"
    try:
        snapshot = Snapshot(Path(context.snapshot), context.repo_map.tree, Limits(), Redactor())
        if snapshot.digest != context.metrics.get("snapshot_digest"):
            raise ValueError("runtime_source_changed")
        emit(context, stage, "started", "initial_intent_validation")
        await connector.validate_intent(context.intent, fingerprint(context.intent))
        emit(context, stage, "ok", "initial_intent_approved")
        stage = "patch"
        emit(context, stage, "started", "initial_patch_started")
        directory = Path(context.output_dir) / "initial-patch"
        manifest = patch_snapshot(context.snapshot, context.repo_map, context.plan, directory)
        files = tuple(sorted(set(context.repo_map.tree) | {c["path"] for c in manifest["changes"]}))
        candidate = PatchedCandidate(directory, manifest, context.plan, files)
        _assert_patch(candidate, context.repo_map)
        approval = await connector.validate_patch(candidate, candidate.fingerprint)
        store.save_validated_patch(context.deployment_id, candidate, context.repo_map, approval, "initial")
        emit(context, stage, "ok", "initial_patch_approved")
        stage = "build"
        emit(context, stage, "started", "initial_build_started")
        built = await connector.build(candidate, candidate.fingerprint)
        _assert_patch(candidate, context.repo_map)
        if built.artifact.source_revision != context.repo_map.commit or built.artifact.target.value != "local":
            raise ValueError("runtime_artifact_binding_mismatch")
        emit(context, stage, "ok", "initial_image_built")
        stage = "start"
        emit(context, stage, "started", "initial_local_started")
        checked = await connector.check_local(built.artifact, context.plan)
        if checked.ok:
            store.mark_patch_applied(context.deployment_id, "initial")
            emit(context, "health", "ok", "initial_health_passed")
            emit(context, "smoke", "ok", "initial_smoke_passed", checked.url)
            finish_run(store, context.deployment_id, "succeeded")
            return 0
        retry_store = RetryStore(Path(context.output_dir) / "retry-state.sqlite")
        previous = Metrics(**{k: v for k, v in context.metrics.items() if k in {f.name for f in fields(Metrics)}})
        async with analysis_session(context.analysis_backend, context.analysis_model, context.replay) as backend:
            result = await recover_local(deployment_id=context.deployment_id, repo_map=context.repo_map,
                snapshot_dir=Path(context.snapshot), previous_intent=context.intent, previous_plan=context.plan,
                previous_patch=PatchReference.from_manifest(manifest), failure=checked.failure,
                backend=backend, limits=analysis_limits(context.analysis_backend), hooks=connector.hooks(), store=retry_store,
                output_dir=Path(context.output_dir) / "retry", previous_metrics=previous, emit=send,
                clarify_requirements=requirements_clarifier(context.snapshot, context.repo_map))
        if result.reanalysis_metrics is not None:
            record_analysis_diagnostics(Path(context.output_dir) / "recovery-analysis-diagnostics.json",
                asdict(result.reanalysis_metrics), result.reanalysis_diagnostics,
                status="passed" if result.status == "recovered" else "failed", stage="recovery")
        update = {"status": "recovered" if result.status == "recovered" else "failed",
                  "reason": result.reason, "attempts": result.retry_attempts,
                  "metrics": asdict(result.reanalysis_metrics) if result.reanalysis_metrics else
                             {"backend": context.analysis_backend, "model": context.analysis_model}}
        if result.status == "recovered":
            store.save_validated_patch(context.deployment_id, result.patch, context.repo_map,
                Approval(approved=True, fingerprint=result.patch.fingerprint), "recovery")
            store.mark_patch_applied(context.deployment_id, "recovery")
            update["corrected"] = {"repo_map": context.repo_map.model_dump(mode="json"),
                "intent": result.analysis.intent.model_dump(mode="json"),
                "plans": context_plans(context) | {"local": result.plan.model_dump(mode="json")}, "metrics": update["metrics"]}
        store.apply_recovery_analysis(context.deployment_id, update)
        finish_run(store, context.deployment_id, "succeeded" if result.status == "recovered" else "failed")
        return 0 if result.status == "recovered" else 1
    except asyncio.CancelledError:
        finish_run(store, context.deployment_id, "interrupted")
        emit(context, stage, "fail", "local_pipeline_interrupted")
        raise
    except Exception as error:
        finish_run(store, context.deployment_id, "failed")
        emit(context, stage, "fail", public_failure_code(stage, error))
        return 1


async def main_async(context, store):
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    return await deploy(context, store)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--openai", action="store_true", help="Allow the trusted recovery worker to use the team API")
    args = parser.parse_args(argv)
    try:
        context = load_context(args.context)
        if context.analysis_backend == "openai" and not args.openai:
            raise ValueError("explicit_api_recovery_required")
        store = Store(args.database)  # Never recover_interrupted() in a worker.
        return asyncio.run(main_async(context, store))
    except (Exception, asyncio.CancelledError):
        print("local_deployer_stopped", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
