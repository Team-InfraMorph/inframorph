import json
import os
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .contracts import FoundationOutputs, Plan
from .errors import ContractError
from .naming import AppIdentity
from .process import Runner


# linux/amd64 manifest of PostgreSQL 16.15-alpine3.24 (the same digest the AWS
# bootstrap task uses), pulled through the Foundation's Docker Hub proxy.
DB_BOOTSTRAP_IMAGE_PATH = "library/postgres@sha256:1a66d744c1b459e13b05a8fca341da84cb63383e99ce262210efee5a319d4551"

# Resource types only the Foundation may own. An app plan touching any of them is refused.
FOUNDATION_TYPES = {
    "google_project_iam_member", "google_project_iam_binding", "google_project_iam_policy",
    "google_project_service", "google_sql_database_instance", "google_sql_user",
    "google_compute_network", "google_compute_subnetwork", "google_service_networking_connection",
    "google_artifact_registry_repository", "google_compute_backend_service", "google_compute_url_map",
    "google_compute_target_https_proxy", "google_compute_global_forwarding_rule",
    "google_compute_region_network_endpoint_group", "google_certificate_manager_certificate",
}


@dataclass(frozen=True)
class TerraformPlan:
    work_dir: Path
    plan_file: Path
    values: Dict[str, Any]
    summary: Dict[str, int]


def terraform_values(
    plan: Plan,
    foundation: FoundationOutputs,
    identity: AppIdentity,
    deployment_image_uri: str,
    service_image_uri: str,
    migration_command: Optional[str],
    activate_services: bool,
) -> Dict[str, Any]:
    if "@sha256:" not in deployment_image_uri or "@sha256:" not in service_image_uri:
        raise ContractError("Terraform accepts only digest-pinned Artifact Registry image URIs")
    services = [
        {
            "name": service.name,
            "kind": service.kind,
            "cpu": service.cpu,
            "memory": service.memory,
            "port": service.port,
            "health": service.health,
            "public": service.public,
            "command": shlex.split(service.command) if service.command else [],
        }
        for service in plan.services
    ]
    # Only a database app has a migration job; the command is ignored otherwise.
    migration_parts = shlex.split(migration_command) if migration_command and plan.db is not None else []
    if plan.db is not None and not migration_parts:
        raise ContractError(
            "DB deployment requires an explicit migration command from the Code Patch/Builder handoff"
        )
    db = plan.db is not None
    return {
        "project_id": foundation.project_id,
        "region": foundation.region,
        "app_id": identity.app_id,
        "resource_prefix": identity.resource_prefix,
        "runtime_service_account_id": identity.runtime_service_account_id,
        "source_revision": plan.source_revision,
        "network_id": foundation.network_id,
        "run_subnet_id": foundation.run_subnet_id,
        "deployment_image_uri": deployment_image_uri,
        "service_image_uri": service_image_uri,
        "services": services,
        "app_config": dict(plan.config),
        "service_config": dict(plan.config),
        "db_enabled": db,
        "service_db_enabled": db,
        "cloudsql_private_ip": foundation.cloudsql_private_ip if db else None,
        "cloudsql_port": foundation.cloudsql_port,
        "cloudsql_initial_database": foundation.cloudsql_db_name if db else None,
        "cloudsql_master_username": foundation.cloudsql_master_username if db else None,
        "cloudsql_master_secret_id": foundation.cloudsql_master_secret_id if db else None,
        "db_bootstrap_service_account_email": foundation.db_bootstrap_service_account_email if db else None,
        "db_bootstrap_image": "{}/{}".format(foundation.dockerhub_proxy_url, DB_BOOTSTRAP_IMAGE_PATH) if db else None,
        "app_database_name": identity.database_name if db else None,
        "app_database_role": identity.database_role if db else None,
        "migration_command": migration_parts,
        "storage_enabled": plan.storage is not None,
        "service_storage_enabled": plan.storage is not None,
        "storage_bucket_name": identity.bucket_name(foundation.project_id),
        "activate_services": activate_services,
        "load_balancer_enabled": foundation.load_balancer_enabled,
        "hostname": identity.hostname(foundation.apps_domain) if foundation.apps_domain else None,
    }


class TerraformManager:
    def __init__(self, module_dir: Path, runner: Optional[Runner] = None) -> None:
        self.module_dir = module_dir.resolve()
        self.runner = runner or Runner()

    def prepare(self, work_dir: Path) -> None:
        work_dir.mkdir(parents=True, exist_ok=True)
        # Copy only the checked-in module files. Never delete the caller's
        # directory: it holds the initialized backend from a previous run.
        for source in self.module_dir.iterdir():
            if source.is_file() and (source.suffix == ".tf" or source.name in {".terraform.lock.hcl", "README.md"}):
                shutil.copy2(str(source), str(work_dir / source.name))

    @staticmethod
    def write_values(work_dir: Path, values: Mapping[str, Any]) -> Path:
        path = work_dir / "deployment.auto.tfvars.json"
        path.write_text(json.dumps(values, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(str(path), 0o600)
        return path

    def init(self, work_dir: Path, state_bucket: str, state_prefix: str) -> None:
        if not state_bucket or not state_prefix.startswith("apps/"):
            raise ContractError("state bucket and an apps/<app> state prefix are required")
        self.runner.run(
            [
                "terraform", "init", "-input=false",
                "-backend-config=bucket={}".format(state_bucket),
                "-backend-config=prefix={}".format(state_prefix),
            ],
            cwd=work_dir,
        )

    def plan(self, work_dir: Path, values: Dict[str, Any], refresh: bool = True) -> TerraformPlan:
        self.write_values(work_dir, values)
        plan_file = work_dir / "app.tfplan"
        command = ["terraform", "plan", "-input=false", "-out", str(plan_file)]
        if not refresh:
            # Only right after this process applied the same state: nothing can
            # have drifted, and refreshing every resource is pure latency.
            command.insert(2, "-refresh=false")
        self.runner.run(command, cwd=work_dir)
        shown = self.runner.json(["terraform", "show", "-json", str(plan_file)], cwd=work_dir)
        summary = {"create": 0, "update": 0, "delete": 0, "replace": 0}
        forbidden = []
        for change in shown.get("resource_changes", []):
            actions = change.get("change", {}).get("actions", [])
            if actions == ["create"]:
                summary["create"] += 1
            elif actions == ["update"]:
                summary["update"] += 1
            elif actions == ["delete"]:
                summary["delete"] += 1
            elif "delete" in actions and "create" in actions:
                summary["replace"] += 1
            if change.get("type") in FOUNDATION_TYPES and actions != ["no-op"]:
                forbidden.append(str(change.get("address", "")))
        if forbidden:
            raise ContractError("App plan attempts to own Foundation resources: {}".format(forbidden))
        return TerraformPlan(work_dir, plan_file, values, summary)

    def apply(self, planned: TerraformPlan) -> Dict[str, Any]:
        self.runner.run(
            ["terraform", "apply", "-input=false", "-auto-approve", str(planned.plan_file)],
            cwd=planned.work_dir,
        )
        raw = self.runner.json(["terraform", "output", "-json"], cwd=planned.work_dir)
        return {
            key: item.get("value") if isinstance(item, dict) and "value" in item else item
            for key, item in raw.items()
        }


def expected_resource_categories(plan: Plan) -> Dict[str, Any]:
    public_services = [item.name for item in plan.services if item.public]
    private_http = [item.name for item in plan.services if item.kind == "http" and not item.public]
    workers = [item.name for item in plan.services if item.kind == "worker"]
    categories = [
        "per-app runtime service account",
        "digest-pinned Cloud Run services (HTTP)",
    ]
    if workers:
        categories.append("Cloud Run worker pools (workers, no ingress)")
    if plan.db is not None:
        categories.extend([
            "app database URL and password secrets (metadata only)",
            "DB bootstrap and migration Cloud Run jobs",
            "Direct VPC egress to private Cloud SQL",
        ])
    if plan.storage is not None:
        categories.append("private versioned Cloud Storage bucket")
    return {
        "app": plan.app,
        "public_services": public_services,
        "private_http_services": private_http,
        "worker_pools": workers,
        "categories": categories,
        "foundation_changes_expected": 0,
        "mutable_image_tags_in_cloud_run": 0,
        "per_app_load_balancer_resources": 0,
    }
