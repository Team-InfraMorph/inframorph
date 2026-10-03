"""C/E validation and build followed by the real A AWS Adapter, after D approval."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

from schemas import DeployEvent
from analyzer.config import Limits
from analyzer.local_verify import child_environment
from analyzer.redaction import Redactor
from analyzer.snapshot import Snapshot
from analyzer.source_policy import validate_demo_intent, validate_demo_plan
from analyzer.recovery import Approval, PatchedCandidate, _assert_patch
from code_patch import patch_snapshot
from policy_gate.gate import validate_intent, validate_patch, validate_plan
from .policy_results import check as policy_check
from builder.runtime import build
from adapters.aws.contracts import Plan as AwsPlan, BuildArtifact as AwsArtifact, FoundationOutputs
from adapters.aws.deployment import DeploymentOrchestrator, DeploymentRequest
from adapters.aws.errors import ContractError
from adapters.aws.locking import AppLock
from adapters.aws.naming import AppIdentity
from adapters.aws.records import DeploymentRecord
from adapters.aws.terraform import TerraformManager, terraform_values
from .aws_config import app_name, environment, load_config
from .db import Store
from .change_detector import plan_diff
from .local_deploy import load_context, send
from .runtime import context_plans, private_json


ROOT = Path(__file__).resolve().parents[1]
RESOURCE_TYPES = frozenset({
    "aws_cloudwatch_log_group", "aws_ecs_task_definition", "aws_ecs_service",
    "aws_iam_role", "aws_iam_role_policy", "aws_iam_role_policy_attachment",
    "aws_lb_target_group", "aws_lb_listener_rule", "aws_secretsmanager_secret",
    "aws_s3_bucket", "aws_s3_bucket_public_access_block",
    "aws_s3_bucket_server_side_encryption_configuration", "aws_s3_bucket_versioning",
    "aws_security_group", "aws_vpc_security_group_ingress_rule", "aws_vpc_security_group_egress_rule",
})
PUBLIC_FAILURE_CODES = frozenset({
    "aws_plan_destructive_change", "aws_plan_outside_app_module", "aws_plan_unknown_action",
    "aws_worker_removal_requires_approval", "aws_worker_removal_base_mismatch",
})
WORKER_RESOURCE_ADDRESSES = (
    "aws_ecs_service.service", "aws_ecs_task_definition.service",
    "aws_cloudwatch_log_group.service", "aws_security_group.service",
    "aws_vpc_security_group_egress_rule.service_https",
    "aws_vpc_security_group_egress_rule.service_dns_udp",
    "aws_vpc_security_group_egress_rule.service_dns_tcp",
    "aws_vpc_security_group_egress_rule.service_to_rds",
    "aws_vpc_security_group_ingress_rule.rds_from_service",
)


def rollback_removal_approved(context, store, previous, deployment):
    """The rollback POST authorizes only the exact previous successful plan.

    A trigger flag alone is not approval: bind both ends of the rollback to
    persisted history and the AWS record currently protected by the app lock.
    """
    if deployment.get("rollback_of") != previous.deployment_id:
        return False
    source = store.get_deployment(previous.deployment_id)
    if source is None or source["project_id"] != context.project_id:
        return False
    target = store.last_live_for_targets(context.project_id, previous.deployment_id, source["targets"])
    if (target is None
            or source["commit_sha"] != previous.source_revision
            or deployment["commit_sha"] != target["commit_sha"]):
        return False
    restored_plan = store.get_plans(target["id"]).get("aws")
    return (restored_plan is not None
            and restored_plan == store.get_plans(context.deployment_id).get("aws")
            and restored_plan == context.aws_plan.model_dump(mode="json"))


def approved_worker_removals(context, store, previous):
    """Only the named private workers in the approved, current app transition."""
    if previous is None:
        return frozenset()
    current_names = {service.name for service in context.aws_plan.services}
    removed = {s["name"] for s in previous.terraform_values.get("services", [])
               if s.get("kind") == "worker" and s.get("public") is False
               and s.get("port") is None and s["name"] not in current_names}
    if not removed:
        return frozenset()
    deployment = store.get_deployment(context.deployment_id)
    approved = (deployment["approved_at"] and all(
        f"aws: 서비스 {name} 제거" in deployment["approval_reasons"] for name in removed))
    if deployment.get("triggered_by") == "rollback":
        if not rollback_removal_approved(context, store, previous, deployment):
            raise ContractError("aws_worker_removal_base_mismatch")
        approved = True
    if not approved:
        raise ContractError("aws_worker_removal_requires_approval")
    base = store.last_live_for_targets(context.project_id, context.deployment_id, ["aws"])
    identity = AppIdentity.from_app(app_name(context.project_id))
    if (base is None or base["id"] != previous.deployment_id or base["commit_sha"] != previous.source_revision
            or previous.app_id != identity.app_id
            or previous.state_key != identity.state_key):
        raise ContractError("aws_worker_removal_base_mismatch")
    old = store.get_plans(base["id"]).get("aws", {})
    reviewed = {s["name"] for s in old.get("services", [])
                if s.get("kind") == "worker" and s.get("public") is False and s.get("port") is None}
    if not removed <= reviewed or any(previous.service_names.get(name) !=
                                      f"{identity.resource_prefix}-{name}" for name in removed):
        raise ContractError("aws_worker_removal_base_mismatch")
    return frozenset(f"{address}[{json.dumps(name)}]" for name in removed for address in WORKER_RESOURCE_ADDRESSES)


def check_changes(shown, approved_worker_addresses=frozenset()):
    for resource in shown.get("resource_changes", []):
        if resource.get("mode") == "data":
            continue
        address = resource.get("address", "")
        kind = resource.get("type")
        actions = resource.get("change", {}).get("actions", [])
        if kind not in RESOURCE_TYPES or not address.startswith(kind + "."):
            raise ContractError("aws_plan_outside_app_module")
        worker_removal = actions == ["delete"] and address in approved_worker_addresses
        if "delete" in actions and not worker_removal and not ("create" in actions and kind == "aws_ecs_task_definition"):
            raise ContractError("aws_plan_destructive_change")
        if not actions or any(a not in {"create", "update", "delete", "no-op", "read"} for a in actions):
            raise ContractError("aws_plan_unknown_action")


class GuardedTerraform(TerraformManager):
    approved_worker_addresses = frozenset()

    def plan(self, work_dir, values):
        # A rejected new plan must not leave a successful old preview on disk.
        (work_dir / "reviewed-summary.json").unlink(missing_ok=True)
        planned = super().plan(work_dir, values)
        shown = self.runner.json(["terraform", "show", "-json", str(planned.plan_file)], cwd=work_dir)
        check_changes(shown, self.approved_worker_addresses)
        # Store only change counts, never the Terraform state/provider secret data.
        path = work_dir / "reviewed-summary.json"
        path.write_text(json.dumps(planned.summary))
        path.chmod(0o600)
        return planned


class SafeEvents:
    def __init__(self, context, expected_url):
        self.context, self.expected_url = context, expected_url

    def emit(self, step, status, detail=None, duration_ms=None, url=None):
        if url is not None and url.rstrip("/") not in {self.expected_url, self.expected_url + "/health"}:
            raise ValueError("aws_event_url_mismatch")
        # Vendor failures may include credentials or arbitrary logs. Public failure
        # diagnostics are fixed codes; the private report is redacted separately.
        if status == "fail":
            code = (detail or "").removeprefix("ContractError: ")
            public_detail = code if code in PUBLIC_FAILURE_CODES else "aws_adapter_failed"
        else:
            public_detail = Redactor().clean(detail or "")
        event = DeployEvent(deployment_id=self.context.deployment_id, ts=datetime.now(timezone.utc),
                            target="aws", step=step, status=status, detail=public_detail,
                            duration_ms=duration_ms, url=self.expected_url if url else None)
        send(event)


def bind(context, store):
    deployment = store.get_deployment(context.deployment_id)
    initial = store.get_deployment_analysis(context.deployment_id)
    if (deployment is None or deployment["project_id"] != context.project_id or
            deployment["status"] != "DEPLOYING" or
            "aws" not in context.targets or set(deployment["targets"]) != set(context.targets) or
            initial is None or initial["initial"]["intent"] != context.intent.model_dump(mode="json") or
            initial["initial"]["repo_map"] != context.repo_map.model_dump(mode="json") or
            initial["initial"]["plans"] != context_plans(context) or
            store.get_plans(context.deployment_id).get("aws") != context.aws_plan.model_dump(mode="json")):
        raise ValueError("aws_context_not_approved")
    if not deployment["approved_at"] and deployment["triggered_by"] != "rollback":
        plans = context_plans(context)
        old = store.last_live_plans(context.project_id, context.deployment_id, plans)
        if "aws" not in old or plan_diff(old, plans):
            raise ValueError("aws_context_not_approved")


def claim(store, context):
    # A different target can run alongside Local; the same AWS deployment cannot
    # execute twice, including after a process restart.
    with store._lock, store._conn:
        store._conn.execute("CREATE TABLE IF NOT EXISTS aws_runtime_runs (deployment_id TEXT PRIMARY KEY, source_revision TEXT NOT NULL, status TEXT NOT NULL)")
        if not store._conn.execute("INSERT OR IGNORE INTO aws_runtime_runs VALUES (?, ?, 'running')",
                                  (context.deployment_id, context.repo_map.commit)).rowcount:
            raise ValueError("aws_runtime_already_started")


def deploy(context, store, config):
    bind(context, store)
    claim(store, context)
    plan = context.aws_plan
    folder = Path(context.output_dir) / "aws"
    folder.mkdir(mode=0o700)
    cloud_plan = AwsPlan.parse(plan.model_dump(mode="json") | {"app": app_name(context.project_id)})
    foundation = FoundationOutputs.from_file(Path(config.foundation), config.account_id, config.region)
    identity = AppIdentity.from_app(cloud_plan.app)
    events = SafeEvents(context, "https://" + identity.hostname(foundation.apps_domain))
    stage = "policy"
    try:
        snapshot = Snapshot(Path(context.snapshot), context.repo_map.tree, Limits(), Redactor())
        if snapshot.digest != context.metrics.get("snapshot_digest"):
            raise ValueError("runtime_source_changed")
        validate_demo_intent(context.intent, context.snapshot, context.repo_map)
        validate_demo_plan(plan, context.repo_map, target="aws")
        policy_check(store, context.deployment_id, "aws", "intent", lambda: validate_intent(context.intent, context.snapshot, context.repo_map.commit))
        policy_check(store, context.deployment_id, "aws", "plan", lambda: validate_plan(context.intent, plan))
        events.emit("policy", "ok", "aws_intent_source_approved")
        stage = "patch"
        events.emit(stage, "started", "aws_patch_started")
        bundle = folder / "patch"
        manifest = patch_snapshot(context.snapshot, context.repo_map, plan, bundle)
        files = tuple(sorted(set(context.repo_map.tree) | {c["path"] for c in manifest["changes"]}))
        candidate = PatchedCandidate(bundle, manifest, plan, files)
        policy_check(store, context.deployment_id, "aws", "patch", lambda: validate_patch(context.snapshot, bundle, plan))
        _assert_patch(candidate, context.repo_map)
        store.save_validated_patch(context.deployment_id, candidate, context.repo_map,
                                   Approval(approved=True, fingerprint=candidate.fingerprint), "initial")
        events.emit(stage, "ok", "aws_patch_approved")
        stage = "build"
        artifact = policy_check(store, context.deployment_id, "aws", "build", lambda: build(context.snapshot, bundle, plan, deployment_id=context.deployment_id, sink=send))
        _assert_patch(candidate, context.repo_map)
        aws_artifact = AwsArtifact.parse(artifact.model_dump(mode="json"))
        private_json(folder / "build.aws.json", artifact.model_dump(mode="json"))
        actual_plan = plan.model_dump(mode="json") | {"app": cloud_plan.app}
        private_json(folder / "plan.aws.json", actual_plan)
        state = Path(context.state_root) / context.project_id / "aws"
        if any(p.is_symlink() for p in (state, *state.parents)):
            raise ValueError("aws_state_symlink")
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.umask(0o077)
        # The build/Analyzer/B never receive these AWS profile references. Add
        # them only after source, patch and image provenance have been verified.
        safe = environment(config, child_environment())
        Path(safe["TF_PLUGIN_CACHE_DIR"]).mkdir(mode=0o700, exist_ok=True)
        endpoint = subprocess.check_output(["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
                                           env=child_environment(), text=True).strip()
        if not endpoint.startswith("unix://"):
            raise ValueError("aws_docker_endpoint_invalid")
        docker_config = state / "docker-auth"
        docker_config.mkdir(mode=0o700, exist_ok=True)
        safe.update(DOCKER_HOST=endpoint, DOCKER_CONFIG=str(docker_config))
        os.environ.clear()
        os.environ.update(safe)
        manager = GuardedTerraform(ROOT / "terraform/app")
        orchestrator = DeploymentOrchestrator(cloud_plan, aws_artifact, foundation, manager, events=events)
        request = DeploymentRequest(context.deployment_id, config.state_bucket, config.migration_command,
                                    config.timeout_seconds, False, state / "aws-work", state / "aws-deployment.json")
        stage = "plan"
        events.emit(stage, "started", "aws_plan_review_started")
        with AppLock(state / "aws-deployment.lock"):
            manager.approved_worker_addresses = approved_worker_removals(
                context, store, DeploymentRecord.load(request.record_path))
            orchestrator.aws.verify_caller()
            orchestrator.aws.ensure_listener_priority(identity.listener_priority, identity.hostname(foundation.apps_domain))
            manager.prepare(request.work_dir)
            manager.init(request.work_dir, config.state_bucket, identity.state_key, foundation.region)
            # Review a real provider plan before any ECR push or application apply.
            preview_uri = foundation.ecr_repository_url + "@sha256:" + "0" * 64
            preview_values = terraform_values(cloud_plan, foundation, identity, preview_uri, preview_uri,
                                             config.migration_command, False)
            preview = manager.plan(request.work_dir, preview_values)
            events.emit("plan", "ok", json.dumps({"app": cloud_plan.app, "changes": preview.summary,
                                                   "foundation_changes": 0}))
            stage = "infra"
            record = orchestrator.deploy(request)
            # Preserve successful inputs and a deployment-specific record for review.
            for filename, value in (("plan.aws.json", actual_plan), ("build.aws.json", artifact.model_dump(mode="json"))):
                temp = state / (filename + ".tmp")
                if temp.exists():
                    raise ValueError("aws_state_write_conflict")
                private_json(temp, value)
                temp.replace(state / filename)
            record.save(folder / "aws-deployment.json")
        store.mark_patch_applied(context.deployment_id, "initial", "aws")
        with store._lock, store._conn:
            store._conn.execute("UPDATE aws_runtime_runs SET status='succeeded' WHERE deployment_id=?", (context.deployment_id,))
        return 0
    except Exception as error:
        # No rejected model/source payload or secret is exposed through D events.
        detail = Redactor().clean(str(error))[-4000:]
        private_json(folder / "failure.json", {"stage": stage, "type": type(error).__name__, "detail": detail})
        with store._lock, store._conn:
            store._conn.execute("UPDATE aws_runtime_runs SET status='failed' WHERE deployment_id=?", (context.deployment_id,))
        events.emit(stage, "fail", str(error))
        return 1
    finally:
        # ECR login tokens are temporary; retain Terraform/record data, remove only
        # the exact private login file created by this deployment worker.
        if "docker_config" in locals():
            login = docker_config / "config.json"
            if login.is_file() and not login.is_symlink():
                login.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--aws-config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.aws_config)
        context = load_context(args.context)
        return deploy(context, Store(args.database), config)
    except Exception:
        print("aws_deployer_stopped", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
