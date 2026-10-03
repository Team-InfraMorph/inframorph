"""C/E validation and build followed by the real GCP Adapter, after D approval."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

from schemas import DeployEvent
from analyzer.config import Limits
from analyzer.local_verify import child_environment
from analyzer.redaction import Redactor
from analyzer.snapshot import Snapshot
from analyzer.source_policy import validate_demo_intent, validate_demo_plan
from analyzer.recovery import Approval, PatchedCandidate, _assert_patch
from code_patch import patch_snapshot
from policy_gate.gate import validate_intent, validate_patch, validate_plan
from builder.runtime import build
from adapters.gcp.contracts import BuildArtifact as GcpArtifact, FoundationOutputs, Plan as GcpPlan
from adapters.gcp.deployment import DeploymentOrchestrator, DeploymentRequest
from adapters.gcp.errors import ContractError
from adapters.gcp.gcp_api import GcpApi
from adapters.gcp.image import ImagePublisher
from adapters.gcp.locking import AppLock
from adapters.gcp.naming import AppIdentity
from adapters.gcp.process import Runner
from adapters.gcp.records import DeploymentRecord
from adapters.gcp.terraform import TerraformManager, terraform_values

from .change_detector import plan_diff
from .db import Store
from .gcp_config import app_name, environment, load_config
from .local_deploy import load_context, send
from .policy_results import check as policy_check
from .runtime import context_plans, private_json


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_FAILURE_CODES = frozenset({
    "gcp_plan_destructive_change", "gcp_plan_outside_app_module",
    "gcp_plan_unknown_action", "gcp_context_not_approved",
})
RESOURCE_TYPES = frozenset({
    "google_cloud_run_v2_job", "google_cloud_run_v2_service", "google_cloud_run_v2_worker_pool",
    "google_secret_manager_secret", "google_secret_manager_secret_iam_member",
    "google_secret_manager_secret_version",
    "google_service_account", "google_storage_bucket", "google_storage_bucket_iam_member",
})


REPLACEABLE_TYPES = frozenset({"google_cloud_run_v2_job"})


def check_changes(shown):
    for resource in shown.get("resource_changes", []):
        if resource.get("mode") == "data":
            continue
        address = resource.get("address", "")
        kind = resource.get("type")
        actions = resource.get("change", {}).get("actions", [])
        if kind not in RESOURCE_TYPES or not address.startswith(kind + "."):
            raise ContractError("gcp_plan_outside_app_module")
        # A Cloud Run job is a stateless one-off definition (bootstrap, migration).
        # A failed create leaves it tainted, so the retry replaces it; nothing is
        # lost. Every other delete or replace still stops before apply.
        replaceable = kind in REPLACEABLE_TYPES and sorted(actions) == ["create", "delete"]
        if "delete" in actions and not replaceable:
            raise ContractError("gcp_plan_destructive_change")
        if not actions or any(action not in {"create", "update", "no-op", "read", "delete"} for action in actions):
            raise ContractError("gcp_plan_unknown_action")


class GuardedTerraform(TerraformManager):
    def plan(self, work_dir, values, refresh=True):
        planned = super().plan(work_dir, values, refresh=refresh)
        shown = self.runner.json(["terraform", "show", "-json", str(planned.plan_file)], cwd=work_dir)
        check_changes(shown)
        return planned


class SafeEvents:
    def __init__(self, context, expected_url=None):
        self.context = context
        self.expected_url = expected_url.rstrip("/") if expected_url else None

    def _url(self, value):
        if value is None:
            return None
        clean = value.rstrip("/")
        parsed = urlsplit(clean)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("gcp_event_url_mismatch")
        base = clean.removesuffix("/health")
        if self.expected_url:
            if base != self.expected_url:
                raise ValueError("gcp_event_url_mismatch")
        elif not parsed.hostname or not parsed.hostname.endswith(".run.app"):
            raise ValueError("gcp_event_url_mismatch")
        return base

    def emit(self, step, status, detail=None, duration_ms=None, url=None):
        checked_url = self._url(url)
        if status == "fail":
            code = (detail or "").removeprefix("ContractError: ")
            public_detail = code if code in PUBLIC_FAILURE_CODES else "gcp_adapter_failed"
        else:
            public_detail = Redactor().clean(detail or "")
        event = DeployEvent(
            deployment_id=self.context.deployment_id,
            ts=datetime.now(timezone.utc), target="gcp", step=step, status=status,
            detail=public_detail, duration_ms=duration_ms, url=checked_url,
        )
        send(event)


def bind(context, store):
    deployment = store.get_deployment(context.deployment_id)
    initial = store.get_deployment_analysis(context.deployment_id)
    if (deployment is None or deployment["project_id"] != context.project_id or
            deployment["status"] != "DEPLOYING" or "gcp" not in context.targets or
            set(deployment["targets"]) != set(context.targets) or context.gcp_plan is None or
            initial is None or initial["initial"]["intent"] != context.intent.model_dump(mode="json") or
            initial["initial"]["repo_map"] != context.repo_map.model_dump(mode="json") or
            initial["initial"]["plans"] != context_plans(context) or
            store.get_plans(context.deployment_id).get("gcp") != context.gcp_plan.model_dump(mode="json")):
        raise ValueError("gcp_context_not_approved")
    if not deployment["approved_at"] and deployment["triggered_by"] != "rollback":
        plans = context_plans(context)
        old = store.last_live_plans(context.project_id, context.deployment_id, plans)
        if "gcp" not in old or plan_diff(old, plans):
            raise ValueError("gcp_context_not_approved")


def claim(store, context):
    with store._lock, store._conn:
        store._conn.execute(
            "CREATE TABLE IF NOT EXISTS gcp_runtime_runs "
            "(deployment_id TEXT PRIMARY KEY, source_revision TEXT NOT NULL, status TEXT NOT NULL)"
        )
        if not store._conn.execute(
            "INSERT OR IGNORE INTO gcp_runtime_runs VALUES (?, ?, 'running')",
            (context.deployment_id, context.repo_map.commit),
        ).rowcount:
            raise ValueError("gcp_runtime_already_started")


def deploy(context, store, config):
    bind(context, store)
    claim(store, context)
    plan = context.gcp_plan
    folder = Path(context.output_dir) / "gcp"
    folder.mkdir(mode=0o700)
    cloud_plan = GcpPlan.parse(plan.model_dump(mode="json") | {"app": app_name(context.project_id)})
    foundation = FoundationOutputs.from_file(Path(config.foundation), config.project_id, config.region)
    identity = AppIdentity.from_app(cloud_plan.app)
    expected_url = ("https://" + identity.hostname(foundation.apps_domain)
                    if foundation.apps_domain else None)
    events = SafeEvents(context, expected_url)
    stage = "policy"
    try:
        snapshot = Snapshot(Path(context.snapshot), context.repo_map.tree, Limits(), Redactor())
        if snapshot.digest != context.metrics.get("snapshot_digest"):
            raise ValueError("runtime_source_changed")
        validate_demo_intent(context.intent, context.snapshot, context.repo_map)
        validate_demo_plan(plan, context.repo_map, target="gcp")
        policy_check(store, context.deployment_id, "gcp", "intent",
                     lambda: validate_intent(context.intent, context.snapshot, context.repo_map.commit))
        policy_check(store, context.deployment_id, "gcp", "plan",
                     lambda: validate_plan(context.intent, plan))
        events.emit("policy", "ok", "gcp_intent_source_approved")

        stage = "patch"
        events.emit(stage, "started", "gcp_patch_started")
        bundle = folder / "patch"
        # Same as AWS: a Local-tested policy repair is carried over, never regenerated.
        from .auto_repair import approved_local_patch
        manifest = approved_local_patch(store, context, plan, bundle)
        if manifest is None:
            manifest = patch_snapshot(context.snapshot, context.repo_map, plan, bundle)
        files = tuple(sorted(set(context.repo_map.tree) | {item["path"] for item in manifest["changes"]}))
        candidate = PatchedCandidate(bundle, manifest, plan, files)
        policy_check(store, context.deployment_id, "gcp", "patch",
                     lambda: validate_patch(context.snapshot, bundle, plan))
        _assert_patch(candidate, context.repo_map)
        store.save_validated_patch(context.deployment_id, candidate, context.repo_map,
                                   Approval(approved=True, fingerprint=candidate.fingerprint), "initial")
        events.emit(stage, "ok", "gcp_patch_approved")

        stage = "build"
        artifact = policy_check(
            store, context.deployment_id, "gcp", "build",
            lambda: build(context.snapshot, bundle, plan,
                          deployment_id=context.deployment_id, sink=send),
        )
        _assert_patch(candidate, context.repo_map)
        gcp_artifact = GcpArtifact.parse(artifact.model_dump(mode="json"))
        private_json(folder / "build.gcp.json", artifact.model_dump(mode="json"))
        actual_plan = plan.model_dump(mode="json") | {"app": cloud_plan.app}
        private_json(folder / "plan.gcp.json", actual_plan)

        state = Path(context.state_root) / context.project_id / "gcp"
        if any(part.is_symlink() for part in (state, *state.parents)):
            raise ValueError("gcp_state_symlink")
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.umask(0o077)
        safe = environment(config, child_environment())
        Path(safe["TF_PLUGIN_CACHE_DIR"]).mkdir(mode=0o700, exist_ok=True)
        endpoint = subprocess.check_output(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            env=child_environment(), text=True,
        ).strip()
        if not endpoint.startswith("unix://"):
            raise ValueError("gcp_docker_endpoint_invalid")
        docker_config = state / "docker-auth"
        docker_config.mkdir(mode=0o700, exist_ok=True)
        safe.update(DOCKER_HOST=endpoint, DOCKER_CONFIG=str(docker_config))
        os.environ.clear()
        os.environ.update(safe)

        runner = Runner(safe)
        manager = GuardedTerraform(ROOT / "terraform/gcp/app", runner)
        publisher = ImagePublisher(runner)
        gcp = GcpApi(foundation, runner)
        orchestrator = DeploymentOrchestrator(
            cloud_plan, gcp_artifact, foundation, manager,
            publisher=publisher, gcp=gcp, events=events,
        )
        request = DeploymentRequest(
            context.deployment_id, config.state_bucket, config.migration_command,
            config.timeout_seconds, False, state / "gcp-work", state / "gcp-deployment.json",
        )

        stage = "plan"
        events.emit(stage, "started", "gcp_plan_review_started")
        with AppLock(state / "gcp-deployment.lock"):
            gcp.verify_caller()
            local_image = publisher.inspect(gcp_artifact)
            manager.prepare(request.work_dir)
            manager.init(request.work_dir, config.state_bucket, identity.state_prefix)
            preview_uri = publisher.repository(foundation) + "@sha256:" + "0" * 64
            # A redeploy keeps its live services in the preview; planning them
            # inactive would show (and the guard would block) deleting them.
            live = DeploymentRecord.load(request.record_path) is not None
            preview_values = terraform_values(
                cloud_plan, foundation, identity, preview_uri, preview_uri,
                config.migration_command, live,
            )
            preview = manager.plan(request.work_dir, preview_values)
            events.emit("plan", "ok", json.dumps({
                "app": cloud_plan.app, "changes": preview.summary,
                "foundation_changes": 0,
            }))
            stage = "infra"
            from .policy_lifecycle import guard
            guard(store, context.deployment_id)
            record = orchestrator.deploy(request)
            for filename, value in (
                ("plan.gcp.json", actual_plan),
                ("build.gcp.json", artifact.model_dump(mode="json")),
            ):
                temporary = state / (filename + ".tmp")
                if temporary.exists():
                    raise ValueError("gcp_state_write_conflict")
                private_json(temporary, value)
                temporary.replace(state / filename)
            record.save(folder / "gcp-deployment.json")

        from .policy_lifecycle import receipt
        receipt(store, context.deployment_id, "gcp", dict(
            snapshot=context.snapshot, bundle=str(bundle),
            repo_map=context.repo_map.model_dump(mode="json"),
            intent=context.intent.model_dump(mode="json"), plan=plan.model_dump(mode="json"),
            artifact=artifact.model_dump(mode="json"), image_id=record.local_image_id,
            published_digest=record.image_digest_uri,
            **{key: manifest[key] for key in ("original_digest", "patched_digest", "diff_sha256")},
        ))
        store.mark_patch_applied(context.deployment_id, "initial", "gcp")
        with store._lock, store._conn:
            store._conn.execute(
                "UPDATE gcp_runtime_runs SET status='succeeded' WHERE deployment_id=?",
                (context.deployment_id,),
            )
        return 0
    except Exception as error:
        detail = Redactor().clean(str(error))[-4000:]
        private_json(folder / "failure.json", {
            "stage": stage, "type": type(error).__name__, "detail": detail,
        })
        with store._lock, store._conn:
            store._conn.execute(
                "UPDATE gcp_runtime_runs SET status='failed' WHERE deployment_id=?",
                (context.deployment_id,),
            )
        events.emit(stage, "fail", str(error))
        return 1
    finally:
        if "docker_config" in locals():
            login = docker_config / "config.json"
            if login.is_file() and not login.is_symlink():
                login.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--gcp-config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.gcp_config)
        context = load_context(args.context)
        return deploy(context, Store(args.database), config)
    except Exception:
        print("gcp_deployer_stopped", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
