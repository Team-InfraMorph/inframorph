import json
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .aws_api import AwsApi
from .contracts import BuildArtifact, FoundationOutputs, Plan
from .errors import AdapterError, ContractError, DeploymentError
from .events import EventEmitter
from .image import ImagePublisher
from .mode import DeployMode, detect_mode
from .naming import AppIdentity
from .records import DeploymentRecord
from .database import DatabaseReceipt, fingerprint
from .smoke import external_https_smoke
from .terraform import TerraformManager, TerraformPlan, terraform_values


@dataclass(frozen=True)
class DeploymentRequest:
    deployment_id: str
    state_bucket: str
    migration_command: Optional[str]
    timeout_seconds: int
    migration_backward_compatible: bool
    work_dir: Path
    record_path: Path


class DeploymentOrchestrator:
    def __init__(
        self,
        plan: Plan,
        artifact: BuildArtifact,
        foundation: FoundationOutputs,
        terraform: TerraformManager,
        publisher: Optional[ImagePublisher] = None,
        aws: Optional[AwsApi] = None,
        events: Optional[EventEmitter] = None,
    ) -> None:
        self.plan = plan
        self.artifact = artifact
        self.foundation = foundation
        self.identity = AppIdentity.from_app(plan.app)
        self.terraform = terraform
        self.publisher = publisher or ImagePublisher()
        self.aws = aws or AwsApi(foundation)
        self.events = events

    def _emit(self, step: str, status: str, **kwargs: Any) -> None:
        if self.events:
            self.events.emit(step, status, **kwargs)

    def deploy(self, request: DeploymentRequest) -> DeploymentRecord:
        if self.plan.db is not None and not request.migration_command:
            raise ContractError(
                "a DB Plan cannot deploy until Code Patch/Builder provides a migration command"
            )
        previous = DeploymentRecord.load(request.record_path)
        if previous is not None and previous.app_id != self.plan.app:
            raise ContractError("deployment record belongs to a different app")
        if previous is not None:
            if previous.terraform_values.get("service_db_enabled") and self.plan.db is None:
                raise ContractError("removing an app database requires a separate data-lifecycle approval")
            if previous.terraform_values.get("service_storage_enabled") and self.plan.storage is None:
                raise ContractError("removing app storage requires a separate data-lifecycle approval")

        current_step = "build"
        mutation_attempted = False
        local_image = None
        published = None
        try:
            self._emit("build", "started", detail="validating exact local linux/amd64 image")
            local_image = self.publisher.inspect(self.artifact)
            self._emit(
                "build", "ok",
                detail=json.dumps({"source_revision": self.plan.source_revision, "local_image_id": local_image.image_id}),
            )

            self.aws.verify_caller()
            self.aws.ensure_listener_priority(
                self.identity.listener_priority,
                self.identity.hostname(self.foundation.apps_domain),
            )

            # Decide first / resume / redeploy from the record AND the real AWS state, before any change.
            current_step = "plan"
            mode = self.detect_mode(previous, request.state_bucket)

            current_step = "push"
            self._emit("push", "started", detail="publishing immutable ECR tag")
            published = self.publisher.publish(local_image, self.artifact, self.foundation)
            self._emit(
                "push", "ok",
                detail=json.dumps({"tag": published.tag, "digest": published.digest}),
            )

            current_step = "infra"
            self._emit("infra", "started", detail=mode.detail(action="planning app-owned resources only"))
            self.terraform.prepare(request.work_dir)
            self.terraform.init(
                request.work_dir,
                request.state_bucket,
                self.identity.state_key,
                self.foundation.region,
            )
            stage_image = previous.image_digest_uri if previous else published.uri
            stage_active = previous is not None
            stage_values = terraform_values(
                self.plan,
                self.foundation,
                self.identity,
                deployment_image_uri=published.uri,
                service_image_uri=stage_image,
                migration_command=request.migration_command,
                activate_services=stage_active,
            )
            if previous is not None:
                # A new service (for example demo-v2's worker) must not start
                # with the previous image before the new migration succeeds.
                stage_values["services"] = previous.terraform_values["services"]
                stage_values["service_config"] = previous.terraform_values["service_config"]
                stage_values["service_db_enabled"] = previous.terraform_values["service_db_enabled"]
                stage_values["service_storage_enabled"] = previous.terraform_values["service_storage_enabled"]
            stage_plan = self.terraform.plan(request.work_dir, stage_values)
            mutation_attempted = True
            stage_outputs = self.terraform.apply(stage_plan)
            self._emit("infra", "ok", detail=json.dumps({"stage_plan": stage_plan.summary}))

            database = None
            if self.plan.db is not None:
                database = self._prepare_database(stage_outputs, request, initialize_secret=(
                    previous is None or not previous.terraform_values.get("service_db_enabled")))

            current_step = "start"
            self._emit("start", "started", detail=json.dumps({
                "code": "aws_services_activating", "database": database,
            }))
            final_values = terraform_values(
                self.plan,
                self.foundation,
                self.identity,
                deployment_image_uri=published.uri,
                service_image_uri=published.uri,
                migration_command=request.migration_command,
                activate_services=True,
            )
            final_plan = self.terraform.plan(request.work_dir, final_values)
            mutation_attempted = True
            final_outputs = self.terraform.apply(final_plan)
            service_names = final_outputs.get("service_names", {})
            if not isinstance(service_names, dict) or not service_names:
                raise DeploymentError("Terraform outputs did not include ECS service names")
            self.aws.wait_services(service_names.values(), request.timeout_seconds)
            self._emit("start", "ok", detail=json.dumps({"services": service_names}))

            current_step = "health"
            self._emit("health", "started", detail="waiting for ALB target health")
            target_group = final_outputs.get("public_target_group_arn")
            if not isinstance(target_group, str) or not target_group:
                raise DeploymentError("Terraform outputs did not include the public target group")
            self.aws.wait_target_healthy(target_group, request.timeout_seconds)
            self._emit("health", "ok", detail="all registered public targets are healthy")

            public_service = next(item for item in self.plan.services if item.public)
            hostname = self.identity.hostname(self.foundation.apps_domain)
            url = "https://{}".format(hostname)
            current_step = "url"
            self._emit("url", "ok", detail=mode.detail(action="external URL is eligible for smoke"), url=url)

            current_step = "smoke"
            self._emit("smoke", "started", detail="performing verified external HTTPS health request")
            smoke = external_https_smoke(hostname, public_service.health or "/", request.timeout_seconds)
            self._emit("smoke", "ok", detail="HTTP {}".format(smoke.status), url=smoke.url)

            record = DeploymentRecord(
                deployment_id=request.deployment_id,
                app_id=self.plan.app,
                source_revision=self.plan.source_revision,
                local_image_id=local_image.image_id,
                image_digest_uri=published.uri,
                task_definitions=dict(final_outputs.get("task_definition_arns", {})),
                service_names=dict(service_names),
                target_group_arn=target_group,
                url=url,
                state_key=self.identity.state_key,
                terraform_values=final_values,
                deploy_mode=mode.mode,
            )
            record.save(request.record_path)
            return record
        except Exception as exc:
            safe_error = "{}: {}".format(type(exc).__name__, str(exc))[:3000]
            self._emit(current_step, "fail", detail=safe_error)
            if mutation_attempted:
                self._recover(previous, request, safe_error)
            raise

    def detect_mode(self, previous: Optional[DeploymentRecord], state_bucket: str) -> DeployMode:
        names = {"{}-{}".format(self.identity.resource_prefix, service.name) for service in self.plan.services}
        if previous is not None:
            names.update(previous.service_names.values())
        return detect_mode(
            self.plan.app,
            previous,
            self.aws.state_exists(state_bucket, self.identity.state_key),
            self.aws.live_services(names),
            self.identity.state_key,
        )

    def _prepare_database(
        self,
        outputs: Dict[str, Any],
        request: DeploymentRequest,
        initialize_secret: bool,
    ) -> Dict[str, Any]:
        started = time.monotonic()
        secret_arn = outputs.get("app_secret_arn")
        data_sg = outputs.get("data_task_security_group_id")
        bootstrap_task = outputs.get("bootstrap_task_definition_arn")
        migration_task = outputs.get("migration_task_definition_arn")
        if not all(isinstance(item, str) and item for item in (secret_arn, data_sg, bootstrap_task, migration_task)):
            raise DeploymentError("database Terraform outputs are incomplete")
        receipt = DatabaseReceipt(request.record_path.parent / "database-bootstrap.json")
        if initialize_secret:
            receipt.clear()
            password = secrets.token_urlsafe(36)
            encoded_user = urllib.parse.quote(self.identity.database_role, safe="")
            encoded_password = urllib.parse.quote(password, safe="")
            database_url = "postgresql://{}:{}@{}:{}/{}?sslmode=require".format(
                encoded_user,
                encoded_password,
                self.foundation.rds_address,
                self.foundation.rds_port,
                self.identity.database_name,
            )
            self.aws.put_secret(
                secret_arn,
                {
                    "engine": "postgres",
                    "host": self.foundation.rds_address,
                    "port": self.foundation.rds_port,
                    "dbname": self.identity.database_name,
                    "username": self.identity.database_role,
                    "password": password,
                    "url": database_url,
                },
            )
        log_group = "/inframorph/apps/{}/data-tasks".format(self.plan.app)
        bootstrap_key = fingerprint({
            "app": self.plan.app, "secret": secret_arn,
            "secret_version": self.aws.current_secret_version(secret_arn),
            "task_definition": bootstrap_task,
            "database": self.identity.database_name, "role": self.identity.database_role,
            "host": self.foundation.rds_address, "port": self.foundation.rds_port,
        })
        bootstrap = receipt.run("bootstrap", bootstrap_key, lambda: self.aws.run_task(
            bootstrap_task,
            data_sg,
            self.foundation.private_subnet_ids,
            "database bootstrap",
            request.timeout_seconds,
            log_group=log_group,
            log_prefix="bootstrap",
        ))
        # Even the same image must verify the live DB schema on every deployment.
        self.aws.run_task(
            migration_task,
            data_sg,
            self.foundation.private_subnet_ids,
            "database migration",
            request.timeout_seconds,
            log_group=log_group,
            log_prefix="migration",
        )
        return {"bootstrap": bootstrap, "migration": "executed",
                "duration_ms": int((time.monotonic() - started) * 1000)}

    def _recover(
        self,
        previous: Optional[DeploymentRecord],
        request: DeploymentRequest,
        cause: str,
    ) -> None:
        self._emit("rollback", "started", detail="deployment failed; evaluating previous successful state")
        if previous is None:
            self._emit("rollback", "fail", detail="no previous successful deployment exists")
            return
        if self.plan.db is not None and not request.migration_backward_compatible:
            self._emit(
                "rollback", "fail",
                detail="automatic code rollback blocked because migration compatibility was not approved",
            )
            return
        try:
            rollback_plan = self.terraform.plan(request.work_dir, previous.terraform_values)
            outputs = self.terraform.apply(rollback_plan)
            services = outputs.get("service_names", previous.service_names)
            self.aws.wait_services(services.values(), request.timeout_seconds)
            target_group = outputs.get("public_target_group_arn", previous.target_group_arn)
            self.aws.wait_target_healthy(target_group, request.timeout_seconds)
            self._emit(
                "rollback", "ok",
                detail=json.dumps({
                    "restored_digest": previous.image_digest_uri,
                    "previous_task_definitions": previous.task_definitions,
                    "cause": cause,
                }),
                url=previous.url,
            )
        except Exception as rollback_error:
            self._emit(
                "rollback", "fail",
                detail="rollback failed: {}".format(str(rollback_error))[:3000],
            )

    def rollback_record(
        self,
        record: DeploymentRecord,
        work_dir: Path,
        state_bucket: str,
        timeout_seconds: int,
    ) -> Dict[str, Any]:
        if record.app_id != self.plan.app:
            raise ContractError("rollback record belongs to a different app")
        self.terraform.prepare(work_dir)
        self.terraform.init(work_dir, state_bucket, record.state_key, self.foundation.region)
        planned = self.terraform.plan(work_dir, record.terraform_values)
        outputs = self.terraform.apply(planned)
        services = outputs.get("service_names", record.service_names)
        self.aws.wait_services(services.values(), timeout_seconds)
        target_group = outputs.get("public_target_group_arn", record.target_group_arn)
        self.aws.wait_target_healthy(target_group, timeout_seconds)
        return outputs
