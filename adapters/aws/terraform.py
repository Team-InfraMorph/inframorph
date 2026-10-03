import json
import os
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from .contracts import FoundationOutputs, Plan
from .errors import ContractError
from .naming import AppIdentity
from .process import Runner


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
        raise ContractError("Terraform accepts only digest-pinned ECR image URIs")
    services = []
    for service in plan.services:
        services.append(
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
        )
    migration_parts = shlex.split(migration_command) if migration_command else []
    if plan.db is not None and not migration_parts:
        raise ContractError(
            "DB deployment requires an explicit migration command from the Code Patch/Builder handoff"
        )
    return {
        "region": foundation.region,
        "account_id": foundation.account_id,
        "app_id": identity.app_id,
        "resource_prefix": identity.resource_prefix,
        "hostname": identity.hostname(foundation.apps_domain),
        "listener_rule_priority": identity.listener_priority,
        "vpc_id": foundation.vpc_id,
        "private_subnet_ids": list(foundation.private_subnet_ids),
        "alb_security_group_id": foundation.alb_security_group_id,
        "https_listener_arn": foundation.https_listener_arn,
        "ecs_cluster_arn": foundation.ecs_cluster_arn,
        "deployment_image_uri": deployment_image_uri,
        "service_image_uri": service_image_uri,
        "source_revision": plan.source_revision,
        "services": services,
        "app_config": dict(plan.config),
        "service_config": dict(plan.config),
        "db_enabled": plan.db is not None,
        "service_db_enabled": plan.db is not None,
        "rds_address": foundation.rds_address,
        "rds_port": foundation.rds_port,
        "rds_initial_database": foundation.rds_db_name,
        "rds_security_group_id": foundation.rds_security_group_id,
        "rds_master_secret_arn": foundation.rds_master_secret_arn,
        "app_database_name": identity.database_name,
        "app_database_role": identity.database_role,
        "app_secret_name": identity.secret_name,
        "migration_command": migration_parts,
        "storage_enabled": plan.storage is not None,
        "service_storage_enabled": plan.storage is not None,
        "storage_bucket_name": identity.bucket_name(foundation.account_id, foundation.region),
        "activate_services": activate_services,
    }


class TerraformManager:
    def __init__(self, module_dir: Path, runner: Optional[Runner] = None) -> None:
        self.module_dir = module_dir.resolve()
        self.runner = runner or Runner()

    def prepare(self, work_dir: Path) -> None:
        work_dir.mkdir(parents=True, exist_ok=True)
        # Copy only the checked-in module files.  Never recursively delete a
        # caller-supplied directory: it may also contain the initialized backend
        # metadata and reviewed plan from a previous run.
        for source in self.module_dir.iterdir():
            if source.is_file() and (source.suffix == ".tf" or source.name in {".terraform.lock.hcl", "README.md"}):
                shutil.copy2(str(source), str(work_dir / source.name))

    @staticmethod
    def write_values(work_dir: Path, values: Mapping[str, Any]) -> Path:
        path = work_dir / "deployment.auto.tfvars.json"
        path.write_text(json.dumps(values, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(str(path), 0o600)
        return path

    def init(
        self,
        work_dir: Path,
        state_bucket: str,
        state_key: str,
        region: str,
    ) -> None:
        if not state_bucket or not state_key:
            raise ContractError("state bucket and per-app state key are required")
        self.runner.run(
            [
                "terraform", "init", "-input=false",
                "-backend-config=bucket={}".format(state_bucket),
                "-backend-config=key={}".format(state_key),
                "-backend-config=region={}".format(region),
                "-backend-config=use_lockfile=true",
                "-backend-config=encrypt=true",
            ],
            cwd=work_dir,
        )

    def plan(self, work_dir: Path, values: Dict[str, Any]) -> TerraformPlan:
        self.write_values(work_dir, values)
        plan_file = work_dir / "app.tfplan"
        self.runner.run(
            ["terraform", "plan", "-input=false", "-out", str(plan_file)],
            cwd=work_dir,
        )
        shown = self.runner.json(["terraform", "show", "-json", str(plan_file)], cwd=work_dir)
        summary = {"create": 0, "update": 0, "delete": 0, "replace": 0}
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
        # This module must never own a Foundation address.  The prefix check is
        # deliberately strict and operates on the machine-readable plan.
        forbidden = []
        for change in shown.get("resource_changes", []):
            address = str(change.get("address", ""))
            if address.startswith("module.foundation") or ".foundation." in address:
                forbidden.append(address)
        if forbidden:
            raise ContractError("aws_plan_outside_app_module")
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
    private_services = [item.name for item in plan.services if not item.public]
    categories = [
        "app IAM execution/task roles and least-privilege policies",
        "per-service ECS security groups and standalone exact-port rules",
        "per-service CloudWatch log groups",
        "digest-pinned ECS task definitions and private-subnet services",
    ]
    if public_services:
        categories.extend(["public service target group", "HTTPS host listener rule"])
    if plan.db is not None:
        categories.extend([
            "app database secret metadata",
            "private database bootstrap and migration task definitions",
            "standalone RDS 5432 rules from app task security groups",
        ])
    if plan.storage is not None:
        categories.extend([
            "private versioned S3 bucket",
            "S3 public-access block and encryption configuration",
        ])
    return {
        "app": plan.app,
        "public_services": public_services,
        "private_services": private_services,
        "categories": categories,
        "foundation_changes_expected": 0,
        "mutable_image_tags_in_ecs": 0,
        "public_worker_resources": 0,
    }
