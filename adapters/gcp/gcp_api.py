import json
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .contracts import FoundationOutputs
from .errors import CommandError, ContractError, DeploymentError
from .process import Runner


IMPERSONATION_ENV = "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT"
# IAM grants made by the staging apply (secret accessor for a new service
# account) are eventually consistent; the first job execution can lose the race.
IAM_NOT_READY_MARKERS = (
    "permission denied on secret",
    "secretmanager.versions.access",
    "iam.serviceaccounts.actas",
)
IAM_RETRY_ATTEMPTS = 6
IAM_RETRY_DELAY_SECONDS = 10
# Terraform already waits for each Cloud Run operation, so these polls normally
# succeed at once; keep the interval short because it is pure added latency.
POLL_SECONDS = 2
NOT_FOUND_MARKERS = ("not found", "404", "no urls matched", "does not exist")


def _not_found(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in NOT_FOUND_MARKERS)


class GcpApi:
    def __init__(self, foundation: FoundationOutputs, runner: Optional[Runner] = None) -> None:
        self.foundation = foundation
        self.runner = runner or Runner()
        self._sleep = time.sleep

    def _scope(self) -> List[str]:
        return ["--project", self.foundation.project_id, "--region", self.foundation.region]

    def verify_caller(self) -> None:
        """The deployer identity is impersonated and sees the Foundation's project."""
        impersonated = self.runner.env.get(IMPERSONATION_ENV)
        if impersonated != self.foundation.deployer_service_account_email:
            raise ContractError(
                "GCP commands must impersonate the Foundation deployer {}".format(
                    self.foundation.deployer_service_account_email
                )
            )
        value = self.runner.json(
            ["gcloud", "projects", "describe", self.foundation.project_id, "--format=json"]
        )
        number = str(value.get("projectNumber", "")) if isinstance(value, dict) else ""
        if number != self.foundation.project_number:
            raise ContractError(
                "GCP project number {} does not match Foundation {}".format(number, self.foundation.project_number)
            )

    def state_exists(self, bucket: str, state_object: str) -> bool:
        """True when the app Terraform state object exists. Missing is False; other errors raise."""
        result = self.runner.run(
            ["gcloud", "storage", "objects", "describe", "gs://{}/{}".format(bucket, state_object), "--format=json"],
            check=False,
        )
        if result.returncode == 0:
            return True
        text = (result.stderr or result.stdout or "").strip()
        if _not_found(text):
            return False
        raise CommandError("could not check Terraform state {}: {}".format(state_object, text[-500:]))

    def describe_service(self, name: str) -> Optional[Dict[str, Any]]:
        result = self.runner.run(
            ["gcloud", "run", "services", "describe", name, *self._scope(), "--format=json"],
            check=False,
        )
        if result.returncode != 0:
            text = (result.stderr or result.stdout or "").strip()
            if _not_found(text):
                return None
            raise CommandError("could not describe Cloud Run service {}: {}".format(name, text[-500:]))
        return _json(result.stdout)

    def live_services(self, service_names: Iterable[str]) -> Dict[str, int]:
        """Cloud Run services of this app that exist (a deployed revision is serving)."""
        return {name: 1 for name in sorted(set(service_names)) if self.describe_service(name) is not None}

    def put_secret(self, secret_id: str, value: str) -> None:
        self.runner.run(
            ["gcloud", "secrets", "versions", "add", secret_id, "--project", self.foundation.project_id,
             "--data-file=-"],
            input_text=value,
            sensitive=True,
        )

    def run_job(self, job_name: str, label: str) -> None:
        """Execute a Cloud Run job and return once it has finished successfully.

        Unlike Fargate run-task there is no network teardown to wait for: the
        execution completes when the container exits. The job's own task
        timeout bounds the wait.
        """
        command = ["gcloud", "run", "jobs", "execute", job_name, *self._scope(), "--wait", "--format=json"]
        attempt = 0
        while True:
            attempt += 1
            try:
                self.runner.run(command)
                return
            except CommandError as exc:
                propagating = any(marker in str(exc).lower() for marker in IAM_NOT_READY_MARKERS)
                if not propagating or attempt >= IAM_RETRY_ATTEMPTS:
                    raise DeploymentError(
                        "{} job failed: {} | logs: gcloud logging read "
                        "'resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"{}\"' --project {}".format(
                            label, str(exc)[-1500:], job_name, self.foundation.project_id
                        )
                    ) from exc
                self._sleep(IAM_RETRY_DELAY_SECONDS)

    def wait_services(self, service_names: Iterable[str], timeout_seconds: int) -> None:
        """Every HTTP service serves 100% of traffic from its latest, ready revision."""
        names = sorted(set(service_names))
        deadline = time.monotonic() + timeout_seconds
        last_detail = ""
        while time.monotonic() < deadline:
            pending = []
            for name in names:
                service = self.describe_service(name)
                ready, detail = _serving_latest(service)
                if not ready:
                    pending.append("{}: {}".format(name, detail))
            if not pending:
                return
            last_detail = "; ".join(pending)
            self._sleep(POLL_SECONDS)
        raise DeploymentError("Cloud Run services did not become ready: {}".format(last_detail))


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise CommandError("command returned invalid JSON") from exc


def _serving_latest(service: Optional[Mapping[str, Any]]) -> Tuple[bool, str]:
    if service is None:
        return False, "missing"
    status = service.get("status") or {}
    conditions = {item.get("type"): item for item in status.get("conditions", [])}
    ready = conditions.get("Ready", {})
    if ready.get("status") == "False":
        raise DeploymentError("Cloud Run service failed: {}".format(ready.get("message", "not ready")))
    latest_created = status.get("latestCreatedRevisionName")
    latest_ready = status.get("latestReadyRevisionName")
    if ready.get("status") != "True" or not latest_created or latest_created != latest_ready:
        return False, "revision {} not ready".format(latest_created)
    traffic = status.get("traffic") or []
    serving = sum(int(item.get("percent", 0) or 0) for item in traffic
                  if item.get("revisionName") == latest_ready or item.get("latestRevision"))
    if serving != 100:
        return False, "latest revision serves {}%".format(serving)
    return True, "ready"
