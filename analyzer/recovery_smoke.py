"""Fixed-demo Docker recovery harness. B/E callbacks are DEVELOPMENT STAND-INS.

An intentional runtime port mismatch produces a real failed health request.
Recovery replays a recorded model result, passes every callback, rebuilds and
checks notes/images. It never calls a team model API or deploys to AWS.
"""
import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

from schemas import BuildArtifact, Intent, Plan, RepoMap
from code_patch import patch_snapshot
from code_patch.runner import TEMPLATES
from code_patch.local_smoke import PNG, docker, environment, preflight, request, wait_for
from .backend import ReplayBackend, Reply
from .feedback import LocalFailure, PatchReference
from .local_verify import config_review, evaluate, prepare, requirements
from .recovery import Approval, BuiltPatch, LocalCheck, RecoveryHooks, recover_local
from .retry_store import RetryStore
from .config import Limits
from .redaction import Redactor
from schemas.common import ContractModel


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIGESTS = {
    "v1": "7079e6c66742b54e5738606ee350cc62eabfbd68754e57d8e71b617e0acdb707",
    "v2": "9f4cbee094dba51c0e42eda6c5c3b51e624d7e548332cef7f3949258c8a77d7a",
}


class PipelineContext(ContractModel):
    repo_map: RepoMap
    intent: Intent
    plan: Plan


def read_context(path, case):
    with Path(path).open("rb") as source:
        data = source.read(Limits().max_request_bytes + 1)
    if len(data) > Limits().max_request_bytes or Redactor().contains_secret(data.decode()):
        raise ValueError("unsafe_pipeline_context")
    context = PipelineContext.model_validate_json(data)
    mapping, snapshot, _ = prepare(case)
    expected_plan = Plan.model_validate_json((ROOT / "schemas/fixtures" / case / "plan.local.json").read_text())
    if context.repo_map != mapping or context.plan != expected_plan:
        raise ValueError("unreviewed_pipeline_context")
    expected_intent = Intent.model_validate_json((ROOT / "schemas/fixtures" / case / "intent.json").read_text())
    intent, _ = evaluate(context.intent.model_dump_json(), mapping, snapshot)
    if (requirements(intent) != requirements(expected_intent) or intent.unknowns or
            config_review(intent, expected_intent, snapshot)["unreviewed_keys"]):
        raise ValueError("unreviewed_pipeline_intent")
    return context


def deployment_id(value):
    if re.fullmatch(r"[A-Za-z0-9_-]{1,120}", value) is None:
        raise argparse.ArgumentTypeError("invalid deployment id")
    return value


async def run_case(case, output, model_intent=None, *, fail_again=False, deployment_id=None, context=None):
    output.mkdir()
    prefix = "inframorph-recovery-" + uuid.uuid4().hex[:12]
    network, db, web = prefix, prefix + "-db", prefix + "-web"
    volumes, containers, networks = [], [], []
    report = {"case": case, "status": "error", "team_api_called": False, "aws_called": False,
              "scenario": "second_failure" if fail_again else "recovery",
              "scope": "fixed fixtures; recorded model response; development B/E callbacks", "calls": []}
    images = []
    events = []
    try:
        fixture = ROOT / "tests/fixtures/analyzer" / case
        source = output / "original"
        shutil.copytree(fixture / "snapshot", source)
        mapping = RepoMap.model_validate_json((fixture / "repo_map.json").read_text())
        expected = Intent.model_validate_json((ROOT / "schemas/fixtures" / case / "intent.json").read_text())
        plan = Plan.model_validate_json((ROOT / "schemas/fixtures" / case / "plan.local.json").read_text())
        if context:
            previous = read_context(context, case)
            report["pipeline_context_provided"] = True
        manifest = patch_snapshot(source, mapping, plan, output / "initial")
        if manifest["original_digest"] != FIXTURE_DIGESTS[case]:
            raise ValueError("unreviewed_fixture")
        preflight(output / "initial/source", source, manifest)
        initial_image = prefix + ":initial"
        images.append(initial_image)
        await asyncio.to_thread(docker, "build", "--platform", "linux/amd64", "-f",
                                str(TEMPLATES / "Dockerfile.smoke"), "-t", initial_image,
                                str(output / "initial/source"), timeout=600)
        docker("network", "create", "--label", "inframorph.recovery=" + prefix, network)
        networks.append(network)
        for suffix in ("db", "uploads"):
            name = prefix + "-" + suffix
            docker("volume", "create", "--label", "inframorph.recovery=" + prefix, name)
            volumes.append(name)
        containers.append(db)
        docker("run", "-d", "--name", db, "--network", network, "--network-alias", "db",
               "--label", "inframorph.recovery=" + prefix, "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
               "-e", "POSTGRES_USER=inframorph", "-e", "POSTGRES_DB=inframorph",
               "-v", db + ":/var/lib/postgresql/data", "postgres:16-alpine")
        await asyncio.to_thread(wait_for, lambda: "accepting connections" in docker("exec", db, "pg_isready", "-U", "inframorph"))
        db_env = "DATABASE_URL=postgresql://inframorph@db:5432/inframorph"
        schema = prefix + "-schema"
        containers.append(schema)
        docker("run", "--rm", "--name", schema, "--network", network, "-e", db_env,
               initial_image, "./node_modules/.bin/prisma", "db", "push", "--skip-generate", timeout=90)
        containers.append(web)

        def start_web(image, port):
            docker("run", "-d", "--name", web, "--network", network, "--label", "inframorph.recovery=" + prefix,
                   "--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                   "--memory", "512m", "--cpus", "1", "-e", db_env, "-e", "STORAGE_DRIVER=fs", "-e", "PORT=" + str(port),
                   "-p", "127.0.0.1::3000", "-v", prefix + "-uploads:/app/uploads", image)
            return web_url()

        def web_url():
            address = docker("port", web, "3000/tcp")
            if not address.startswith("127.0.0.1:"):
                raise ValueError("unexpected_published_address")
            return "http://" + address

        initial_url = start_web(initial_image, 3100)
        await asyncio.to_thread(wait_for, lambda: "3100" in docker("logs", web), timeout=30)
        try:
            await asyncio.to_thread(request, initial_url + "/health")
            raise ValueError("injected_failure_not_observed")
        except OSError:
            pass
        report["initial_failure_observed"] = True
        failure = LocalFailure(stage="health", code="port_mismatch",
                               log="health check expected port 3000; " + docker("logs", web))
        docker("rm", "-f", web)
        if context:
            previous_intent, previous_plan = previous.intent, previous.plan
        else:
            previous_intent = expected.model_copy(deep=True)
            previous_intent.workloads[0].port = 3100
            previous_intent.config["PORT"] = "3100"
            previous_plan = plan.model_copy(deep=True)
            previous_plan.services[0].port = 3100
            previous_plan.config["PORT"] = "3100"
        corrected = expected
        if model_intent:
            model_map, model_source, _ = prepare(case)
            corrected, _ = evaluate(Path(model_intent).read_text(), model_map, model_source)
            report["recorded_model_intent"] = str(Path(model_intent).resolve())
        # Read all fixture files using the production loop before replaying the
        # recorded Intent. This does not test the model's tool-selection behavior.
        reads = [{"type": "function_call", "call_id": f"read-{i}", "name": "Read",
                  "arguments": json.dumps({"path": path, "start_line": 1, "line_count": 200})}
                 for i, path in enumerate(mapping.tree)]
        backend = ReplayBackend([Reply(output=reads), Reply(text=corrected.model_dump_json())])
        runtime_image = None

        async def validate_intent(intent, signature):
            report["calls"].append("intent_policy")
            return Approval(approved=requirements(intent) == requirements(expected) and not intent.unknowns,
                            fingerprint=signature)

        async def make_plan(intent):
            report["calls"].append("plan")
            result = plan.model_copy(deep=True)
            result.config["PORT"] = str(next(w.port for w in intent.workloads if w.public))
            return result

        async def validate_patch(candidate, signature):
            report["calls"].append("patch_policy")
            await asyncio.to_thread(preflight, candidate.directory / "source", source, candidate.manifest)
            return Approval(approved=True, fingerprint=signature)

        async def build(candidate, signature):
            nonlocal runtime_image
            report["calls"].append("build")
            runtime_image = prefix + ":retry"
            images.append(runtime_image)
            await asyncio.to_thread(docker, "build", "--platform", "linux/amd64", "-f",
                                    str(TEMPLATES / "Dockerfile.smoke"), "-t", runtime_image,
                                    str(candidate.directory / "source"), timeout=600)
            report["rebuilt_image_id"] = docker("image", "inspect", runtime_image, "--format", "{{.Id}}")
            # Common schema identifier; dev adapter resolves it to the unique
            # local tag above so no shared app:<sha> tag is overwritten.
            return BuiltPatch(artifact=BuildArtifact(source_revision=mapping.commit, target="local",
                                                     image=plan.image_tag, platform="linux/amd64"), fingerprint=signature)

        async def check_local(artifact, corrected_plan):
            report["calls"].append("local")
            base = start_web(runtime_image, 3100 if fail_again else int(corrected_plan.config["PORT"]))
            if fail_again:
                await asyncio.to_thread(wait_for, lambda: "3100" in docker("logs", web), timeout=30)
                try:
                    await asyncio.to_thread(request, base + "/health")
                    raise ValueError("second_injected_failure_not_observed")
                except OSError:
                    return LocalCheck(ok=False, failure=LocalFailure(stage="health", code="port_mismatch"))
            await asyncio.to_thread(wait_for, lambda: json.loads(request(base + "/health")).get("status") == "ok")
            note = json.loads(request(base + "/api/notes", data=b'{"text":"recovery persistence"}',
                                      content_type="application/json", expected=201))
            uploaded = json.loads(request(base + "/api/images", data=PNG, content_type="image/png", expected=201))
            if note not in json.loads(request(base + "/api/notes")) or request(base + uploaded["url"]) != PNG:
                return LocalCheck(ok=False, failure=LocalFailure(stage="smoke", code="image_readback_failed"))
            docker("restart", web)
            # Docker may allocate a new host port for an ephemeral binding
            # when the container restarts. Rediscover it before probing.
            base = web_url()
            await asyncio.to_thread(wait_for, lambda: json.loads(request(base + "/health")).get("status") == "ok")
            if note not in json.loads(request(base + "/api/notes")) or request(base + uploaded["url"]) != PNG:
                return LocalCheck(ok=False, failure=LocalFailure(stage="smoke", code="note_roundtrip_failed"))
            report["note_image_restart_checks_passed"] = True
            if any(service.kind.value == "worker" for service in corrected_plan.services):
                worker = prefix + "-worker"
                containers.append(worker)
                docker("run", "-d", "--name", worker, "--network", network,
                       "--label", "inframorph.recovery=" + prefix, "--read-only", "--tmpfs", "/tmp",
                       "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "-e", db_env,
                       runtime_image, "node", "src/worker.js")
                def worker_reads_note():
                    for line in docker("logs", worker).splitlines():
                        try:
                            item = json.loads(line)
                        except ValueError:
                            continue
                        if item.get("event") == "note_count" and item.get("count") == 1:
                            return True
                    return False
                await asyncio.to_thread(wait_for, worker_reads_note, timeout=30)
                if docker("inspect", worker, "--format", "{{.Image}}") != report["rebuilt_image_id"]:
                    raise ValueError("worker_image_binding_mismatch")
                docker("stop", "--time", "15", worker)
                if docker("inspect", worker, "--format", "{{.State.ExitCode}}") != "0":
                    raise ValueError("worker_exit_failed")
                report["worker_reads_same_db_and_exits_cleanly"] = True
            return LocalCheck(ok=True, url=base)

        store = RetryStore(output / "state/retries.sqlite")
        hooks = RecoveryHooks(validate_intent, make_plan, validate_patch, build, check_local)
        def emit(event):
            events.append(event)
            print(event.jsonl(), end="", flush=True)
        args = dict(deployment_id=deployment_id or prefix, repo_map=mapping, snapshot_dir=source, previous_intent=previous_intent,
                    previous_plan=previous_plan, previous_patch=PatchReference.from_manifest(manifest),
                    failure=failure, backend=backend, hooks=hooks, store=store, output_dir=output / "retry", emit=emit)
        result = await recover_local(**args)
        report.update(deployment_id=deployment_id or prefix, recovery_status=result.status,
                      reason=result.reason, retry_attempts=result.retry_attempts,
                      analysis_metrics=asdict(result.analysis.metrics) if result.analysis else None)
        store = RetryStore(output / "state/retries.sqlite")
        duplicate = await recover_local(**(args | {"store": store, "backend": ReplayBackend([])}))
        report["restart_does_not_retry_again"] = duplicate.status == "not_retried" and not duplicate.events
        expected_status = "failed" if fail_again else "recovered"
        if (result.status != expected_status or result.retry_attempts != 1 or not report["restart_does_not_retry_again"] or
                report["calls"] != ["intent_policy", "plan", "patch_policy", "build", "local"]):
            raise ValueError("recovery_assertion_failed")
        if fail_again and result.reason != "second_local_failure":
            raise ValueError("wrong_second_failure_reason")
        if not fail_again and any(e.status == "fail" for e in events):
            raise ValueError("recovered_stream_contains_terminal_failure")
        report["status"] = "passed"
    except Exception as error:
        report["error"] = type(error).__name__
    finally:
        cleanup = []
        for names, operation in ((containers, ["rm", "-f"]), (volumes, ["volume", "rm"]), (networks, ["network", "rm"])):
            for name in reversed(names):
                try:
                    r = subprocess.run(["docker", *operation, name], env=environment(), capture_output=True, timeout=30)
                    if r.returncode and b"No such" not in r.stderr:
                        cleanup.append(name)
                except (OSError, subprocess.SubprocessError):
                    cleanup.append(name)
        report["cleanup_errors"] = cleanup
        report["local_images"] = images
        if cleanup:
            report["status"] = "error"
        (output / "events.jsonl").write_text("".join(e.jsonl() for e in events))
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("v1", "v2", "all"), default="all")
    parser.add_argument("--model-result-dir", type=Path, help="Recorded model directory with v1/v2/intent.json")
    parser.add_argument("--fail-again", action="store_true", help="Expect a second terminal failure, with no third attempt")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--deployment-id", type=deployment_id,
                        help="Emit this Control Plane id; exit 0 only for a recovered deployment")
    parser.add_argument("--pipeline-context", type=Path, help="Reviewed fixture repo_map/Intent/Local Plan from Control Plane")
    args = parser.parse_args(argv)
    if args.case == "all" and (args.deployment_id or args.pipeline_context):
        parser.error("deployment-id/pipeline-context require a single fixture case")
    output = (args.output_dir or ROOT / ".local/recovery-smoke" / uuid.uuid4().hex[:12]).absolute()
    output.mkdir(parents=True, exist_ok=False)
    reports = []
    for case in (("v1", "v2") if args.case == "all" else (args.case,)):
        intent = args.model_result_dir / case / "intent.json" if args.model_result_dir else None
        reports.append(asyncio.run(run_case(case, output / case, intent, fail_again=args.fail_again,
                                           deployment_id=args.deployment_id, context=args.pipeline_context)))
        print(json.dumps({"case": case, "status": reports[-1]["status"], "error": reports[-1].get("error")}), file=sys.stderr, flush=True)
    (output / "summary.json").write_text(json.dumps({"results": reports, "team_api_called": False, "aws_called": False}, indent=2) + "\n")
    passed = all(r["status"] == "passed" for r in reports)
    if args.deployment_id:
        passed = passed and all(r.get("recovery_status") == "recovered" for r in reports)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
