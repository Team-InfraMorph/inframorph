"""Actual E Gate/Builder/Local smoke with one controlled HTTP read fault.

The Planner and model response are fixture replays, explicitly development-only.
The first E smoke receives corrupt bytes after a real image upload/read; this is
fault injection, not a naturally occurring application bug or a model-written fix.
No public tunnel, team API, AWS or unrelated Docker cleanup is used.
"""
import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import shutil
from unittest.mock import patch
import uuid

from code_patch import patch_snapshot
from schemas import Intent, Plan, RepoMap
from .backend import ReplayBackend, Reply
from .e_runtime import EConnector, classify_e_failure
from .feedback import PatchReference
from .local_verify import ROOT, save
from .recovery import recover_local
from .retry_store import RetryStore
from .source_policy import validate_demo_intent


async def run_case(case, output):
    from policy_gate.gate import PolicyError, validate_intent, validate_patch
    from builder.runtime import build, run, RuntimeFailure
    from adapters.local import runtime
    output.mkdir()
    source = output / "snapshot"
    fixture = ROOT / "tests/fixtures/analyzer" / case
    shutil.copytree(fixture / "snapshot", source)
    mapping = RepoMap.model_validate_json((fixture / "repo_map.json").read_text())
    intent = Intent.model_validate_json((ROOT / f"schemas/fixtures/{case}/intent.json").read_text())
    plan = Plan.model_validate_json((ROOT / f"schemas/fixtures/{case}/plan.local.json").read_text())
    namespace = "c-e-check-" + uuid.uuid4().hex[:12]
    deployment_id = "d-" + namespace
    project = "inframorph-" + namespace
    report = {"case": case, "status": "error", "runtime_namespace": namespace,
              "source_revision": mapping.commit, "team_api_called": False, "publish": False,
              "gate_builder_adapter": "actual E modules", "planner": "explicit fixture callback",
              "model": "fixture response replay; model did not select tools",
              "fault_injection": "corrupt one image-read response after real HTTP upload/read"}
    events, initial_events, first_record = [], [], {}
    resources_started = False

    async def make_plan(corrected):
        # Fixture-only Planner, never exported as a product default.
        return plan.model_copy(deep=True)

    connector = EConnector(snapshot=source, repo_map=mapping, state_root=output / "runtime",
                           runtime_name=namespace, deployment_id=deployment_id, make_plan=make_plan)
    try:
        report["phase"] = "initial_gate_and_build"
        existing = run(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + project])
        if existing:
            raise ValueError("runtime_namespace_already_used")
        validate_demo_intent(intent, source, mapping)
        validate_intent(intent, source, mapping.commit)
        manifest = patch_snapshot(source, mapping, plan, output / "initial")
        validate_patch(source, output / "initial", plan)
        artifact = await asyncio.to_thread(build, source, output / "initial", plan)
        report["built_image"] = artifact.image
        runtime_plan = Plan.model_validate(plan.model_dump() | {"app": namespace})
        original_request = runtime.request
        fault_observed = False

        def injected_request(url, **kwargs):
            nonlocal fault_observed
            value = original_request(url, **kwargs)
            if kwargs.get("data") is not None and url.endswith("/api/notes"):
                first_record["note"] = json.loads(value)
            if kwargs.get("data") is not None and url.endswith("/api/images"):
                first_record["image_url"] = json.loads(value)["url"]
            if kwargs.get("data") is None and "/api/images/" in url and not fault_observed:
                fault_observed = True
                return value + b"controlled-read-fault"
            return value

        resources_started = True
        report["phase"] = "initial_smoke_fault"
        with patch.object(runtime, "request", injected_request):
            try:
                await asyncio.to_thread(runtime.deploy, runtime_plan, artifact, connector.state,
                                        publish=False, deployment_id=deployment_id, sink=initial_events.append)
                raise ValueError("expected_initial_smoke_failure")
            except (RuntimeFailure, PolicyError) as error:
                failure_code = str(error)
        if failure_code != "image_persistence_failed" or not fault_observed:
            raise ValueError("unexpected_initial_failure")
        failure = classify_e_failure(failure_code)
        report["initial_e_failure"] = failure_code
        report["initial_classification"] = failure.model_dump(mode="json")
        if run(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + project]):
            raise ValueError("initial_failure_not_cleaned")
        report["initial_failure_containers_cleaned"] = True
        report["phase"] = "recovery"
        replies = [Reply(output=[{"type": "function_call", "call_id": "read-" + str(i), "name": "Read",
                   "arguments": json.dumps({"path": name, "start_line": 1, "line_count": 200})}
                   for i, name in enumerate(mapping.tree)]), Reply(text=intent.model_dump_json())]
        store = RetryStore(output / "retry-state/retries.sqlite")
        args = dict(deployment_id=deployment_id, repo_map=mapping, snapshot_dir=source,
                    previous_intent=intent, previous_plan=plan, previous_patch=PatchReference.from_manifest(manifest),
                    failure=failure, backend=ReplayBackend(replies), hooks=connector.hooks(), store=store,
                    output_dir=output / "retry", emit=events.append)
        result = await recover_local(**args)
        if result.status != "recovered" or result.retry_attempts != 1:
            report["recovery_reason"] = result.reason
            raise ValueError("e_recovery_failed")
        if any(event.status == "fail" for event in result.events):
            raise ValueError("nonterminal_failure_leaked")
        url = connector.last_deployment["url"]
        report["phase"] = "persistence_and_restart"
        await asyncio.to_thread(runtime.smoke, url, record=first_record)
        state = connector.state
        current = json.loads((state / "current.json").read_text())
        compose = runtime.compose_args(state, current["config"])
        await asyncio.to_thread(run, compose + ["restart", "web"])
        web = next(s for s in runtime_plan.services if s.public)
        url = await asyncio.to_thread(runtime.wait_for, lambda: runtime.local_url(run, compose, web))
        await asyncio.to_thread(runtime.wait_for, lambda: runtime.smoke(url, record=first_record))
        report["original_note_image_survive_recovery_and_restart"] = True
        if case == "v2":
            report["phase"] = "worker"
            def worker_reads_database():
                logs = run(compose + ["logs", "--no-color", "worker"])
                if '"event":"note_count"' not in logs.replace(" ", ""):
                    raise RuntimeFailure("worker_not_ready")
                return True
            await asyncio.to_thread(runtime.wait_for, worker_reads_database, timeout=30)
            worker = run(compose + ["ps", "-q", "worker"])
            if run(["docker", "inspect", worker, "--format", "{{.Image}}"] ) != connector.last_deployment["image_id"]:
                raise ValueError("worker_image_mismatch")
            await asyncio.to_thread(run, compose + ["stop", "-t", "15", "worker"])
            if run(["docker", "inspect", worker, "--format", "{{.State.ExitCode}}"]) != "0":
                raise ValueError("worker_exit_failed")
            report["worker_same_image_database_and_clean_exit"] = True
        repeated = await recover_local(**(args | {"store": RetryStore(output / "retry-state/retries.sqlite"),
                                                   "backend": ReplayBackend([])}))
        if repeated.status != "not_retried" or repeated.events:
            raise ValueError("repeated_retry_not_blocked")
        report.update(status="passed", retry_attempts=result.retry_attempts,
                      repeated_retry_blocked=True, analysis_metrics=asdict(result.analysis.metrics), phase="complete")
    except Exception as error:
        # Detailed raw errors may contain app content; use a fixed report code.
        report["error"] = "actual_e_verification_failed"
        import re
        if type(error).__name__ in {"PolicyError", "RuntimeFailure", "SourcePolicyError"} and re.fullmatch(r"[a-z_]{1,80}", str(error)):
            report["dependency_code"] = str(error)
        raise
    finally:
        if resources_started:
            await connector.cleanup()
            # The verifier alone owns this fresh random namespace. Product
            # cleanup preserves volumes; remove only our two test volumes here.
            volumes = run(["docker", "volume", "ls", "--format", "{{.Name}}"]).splitlines()
            for suffix in ("-db", "-uploads"):
                if project + suffix in volumes:
                    await asyncio.to_thread(run, ["docker", "volume", "rm", project + suffix])
            report["test_resources_cleaned"] = not run(["docker", "ps", "-aq", "--filter",
                                                       "label=com.docker.compose.project=" + project])
        save(output / "events.json", [event.model_dump(mode="json") for event in events])
        save(output / "initial-e-events.json", [event.model_dump(mode="json") for event in initial_events])
        save(output / "report.json", report)
    return report


async def main_async(args):
    output = args.output_dir or ROOT / ".local/e-recovery-check" / uuid.uuid4().hex[:12]
    output.mkdir(parents=True, exist_ok=False)
    results = []
    for case in ("v1", "v2") if args.case == "all" else (args.case,):
        result = await run_case(case, output / case)
        results.append(result)
        print(json.dumps({"case": case, "status": result["status"]}), flush=True)
    save(output / "summary.json", {"results": results, "team_api_called": False})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("v1", "v2", "all"), default="all")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        asyncio.run(main_async(args))
    except Exception:
        print(json.dumps({"error": "actual_e_verification_failed"}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
