import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .contracts import BuildArtifact, FoundationOutputs, Plan
from .database import MigrationReceipt, fingerprint
from .errors import CommandError, ContractError, DeploymentError
from .events import EventEmitter
from .gcp_api import GcpApi
from .image import ImagePublisher
from .mode import DeployMode, detect_mode
from .naming import AppIdentity
from .records import DeploymentRecord
from .smoke import external_https_smoke
from .terraform import TerraformManager, terraform_values


PRISMA_SCHEMA_PATH = "/app/prisma/schema.prisma"


@dataclass(frozen=True)
class DeploymentRequest:
    deployment_id: str
    state_bucket: str
    migration_command: Optional[str]
    timeout_seconds: int
    migration_backward_compatible: bool
    work_dir: Path
    record_path: Path


def _require(outputs: Dict[str, Any], key: str) -> Any:
    value = outputs.get(key)
    if value in (None, "", {}, []):
        raise DeploymentError("Terraform outputs did not include {}".format(key))
    return value


class DeploymentOrchestrator:
    """staging apply -> DB bootstrap/migration -> activation apply, as on AWS.

    Deploy-time shortcuts that keep that order intact:
    - no database: there is no migration to protect, so staging is skipped;
    - redeploy of a database app: the app role already exists, so only the
      migration job runs (bootstrap is idempotent but costs a job start);
    - activation right after staging plans without a refresh.
    """

    def __init__(
        self,
        plan: Plan,
        artifact: BuildArtifact,
        foundation: FoundationOutputs,
        terraform: TerraformManager,
        publisher: Optional[ImagePublisher] = None,
        gcp: Optional[GcpApi] = None,
        events: Optional[EventEmitter] = None,
    ) -> None:
        self.plan = plan
        self.artifact = artifact
        self.foundation = foundation
        self.identity = AppIdentity.from_app(plan.app)
        self.terraform = terraform
        self.publisher = publisher or ImagePublisher()
        self.gcp = gcp or GcpApi(foundation)
        self.events = events

    def _emit(self, step: str, status: str, **kwargs: Any) -> None:
        if self.events:
            self.events.emit(step, status, **kwargs)

    def _values(self, request: DeploymentRequest, deployment_uri: str, service_uri: str, active: bool) -> Dict[str, Any]:
        return terraform_values(
            self.plan, self.foundation, self.identity,
            deployment_image_uri=deployment_uri,
            service_image_uri=service_uri,
            migration_command=request.migration_command,
            activate_services=active,
        )

    def deploy(self, request: DeploymentRequest) -> DeploymentRecord:
        if self.plan.db is not None and not request.migration_command:
            raise ContractError("a DB Plan cannot deploy until Code Patch/Builder provides a migration command")
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
        try:
            self._emit("build", "started", detail="validating exact local linux/amd64 image")
            local_image = self.publisher.inspect(self.artifact)
            self._emit("build", "ok", detail=json.dumps({
                "source_revision": self.plan.source_revision, "local_image_id": local_image.image_id,
            }))

            current_step = "plan"
            self.gcp.verify_caller()
            # Decide first / resume / redeploy from the record AND the real GCP state, before any change.
            mode = self.detect_mode(previous, request.state_bucket)

            current_step = "push"
            self._emit("push", "started", detail="publishing immutable Artifact Registry tag")
            published = self.publisher.publish(local_image, self.artifact, self.foundation)
            self._emit("push", "ok", detail=json.dumps({"tag": published.tag, "digest": published.digest}))

            current_step = "infra"
            self._emit("infra", "started", detail=mode.detail(action="planning app-owned resources only"))
            self.terraform.prepare(request.work_dir)
            self.terraform.init(request.work_dir, request.state_bucket, self.identity.state_prefix)
            staged = self.plan.db is not None
            if staged:
                stage_values = self._values(
                    request, published.uri, previous.image_digest_uri if previous else published.uri,
                    active=previous is not None,
                )
                if previous is not None:
                    # A new service (e.g. demo-v2's worker) must not start with the
                    # previous image before the new migration succeeds.
                    for key in ("services", "service_config", "service_db_enabled", "service_storage_enabled"):
                        stage_values[key] = previous.terraform_values[key]
                stage_plan = self.terraform.plan(request.work_dir, stage_values)
                mutation_attempted = True
                stage_outputs = self.terraform.apply(stage_plan)
                self._emit("infra", "ok", detail=json.dumps({"stage_plan": stage_plan.summary}))
                self._prepare_database(stage_outputs, previous, request, self._schema_digest(local_image))
            else:
                self._emit("infra", "ok", detail="no database: staging skipped, activating directly")

            current_step = "start"
            self._emit("start", "started", detail="activating digest-pinned Cloud Run revisions")
            final_values = self._values(request, published.uri, published.uri, active=True)
            final_plan = self.terraform.plan(request.work_dir, final_values, refresh=not staged)
            mutation_attempted = True
            final_outputs = self.terraform.apply(final_plan)
            service_names = _require(final_outputs, "service_names")
            service_urls = _require(final_outputs, "service_urls")
            self._emit("start", "ok", detail=json.dumps({"services": service_names}))

            current_step = "health"
            self._emit("health", "started", detail="waiting for the latest revision to serve all traffic")
            http_names = [service_names[name] for name in service_urls if name in service_names]
            self.gcp.wait_services(http_names, request.timeout_seconds)
            self._emit("health", "ok", detail="latest revisions are ready and serve 100% of traffic")

            url = _require(final_outputs, "external_url")
            current_step = "url"
            self._emit("url", "ok", detail=mode.detail(action="external URL is eligible for smoke"), url=url)

            current_step = "smoke"
            self._emit("smoke", "started", detail="performing verified external HTTPS health request")
            smoke = external_https_smoke(url, self.plan.public_service.health or "/", request.timeout_seconds)
            self._emit("smoke", "ok", detail="HTTP {}".format(smoke.status), url=smoke.url)

            record = DeploymentRecord(
                deployment_id=request.deployment_id,
                app_id=self.plan.app,
                source_revision=self.plan.source_revision,
                local_image_id=local_image.image_id,
                image_digest_uri=published.uri,
                revisions=dict(final_outputs.get("latest_revisions") or {}),
                service_names=dict(service_names),
                url=url,
                state_prefix=self.identity.state_prefix,
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
        # A live app always has its public service, named after the app.
        names = {self.identity.app_id}
        if previous is not None:
            names.update(previous.service_names.values())
        return detect_mode(
            self.plan.app,
            previous,
            self.gcp.state_exists(state_bucket, self.identity.state_object()),
            self.gcp.live_services(names),
            self.identity.state_prefix,
        )

    def _schema_digest(self, image: Any) -> Optional[str]:
        try:
            digest = self.publisher.file_digest(image, PRISMA_SCHEMA_PATH)
        except (CommandError, OSError):
            return None
        return digest if isinstance(digest, str) and len(digest) == 64 else None

    def _prepare_database(
        self,
        outputs: Dict[str, Any],
        previous: Optional[DeploymentRecord],
        request: DeploymentRequest,
        schema_digest: Optional[str],
    ) -> Dict[str, str]:
        # Terraform already wrote the app password and DATABASE_URL versions in
        # the staging apply (Cloud Run rejects jobs whose secrets have no version).
        bootstrap_job = _require(outputs, "bootstrap_job_name")
        migration_job = _require(outputs, "migration_job_name")
        initialize = previous is None or not previous.terraform_values.get("service_db_enabled")
        if initialize:
            self.gcp.run_job(bootstrap_job, "database bootstrap")
        run_migration = lambda: self.gcp.run_job(migration_job, "database migration")
        receipt = MigrationReceipt(request.record_path.with_name("gcp-database-receipt.json"))
        if schema_digest is None:
            receipt.completed = None
            receipt.save()
            run_migration()
            migration = "executed"
        else:
            migration = receipt.run(fingerprint({
                "schema": schema_digest,
                "command": request.migration_command,
                "database": self.identity.database_name,
                "role": self.identity.database_role,
            }), run_migration)
        return {"bootstrap": "executed" if initialize else "reused", "migration": migration}

    def _recover(self, previous: Optional[DeploymentRecord], request: DeploymentRequest, cause: str) -> None:
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
            self._restore(previous.terraform_values, request.work_dir, request.timeout_seconds)
            self._emit(
                "rollback", "ok",
                detail=json.dumps({
                    "restored_digest": previous.image_digest_uri,
                    "previous_revisions": previous.revisions,
                    "cause": cause,
                }),
                url=previous.url,
            )
        except Exception as rollback_error:
            self._emit("rollback", "fail", detail="rollback failed: {}".format(str(rollback_error))[:3000])

    def _restore(self, values: Dict[str, Any], work_dir: Path, timeout_seconds: int) -> Dict[str, Any]:
        planned = self.terraform.plan(work_dir, values)
        outputs = self.terraform.apply(planned)
        names = outputs.get("service_names") or {}
        urls = outputs.get("service_urls") or {}
        self.gcp.wait_services([names[name] for name in urls if name in names], timeout_seconds)
        return outputs

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
        self.terraform.init(work_dir, state_bucket, record.state_prefix)
        return self._restore(record.terraform_values, work_dir, timeout_seconds)
