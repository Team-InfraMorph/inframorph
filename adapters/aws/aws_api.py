import json
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .contracts import FoundationOutputs
from .errors import CommandError, ContractError, DeploymentError
from .process import Runner


# A brand-new IAM role can take a few seconds to become usable by ECS (seen twice on first deploys).
ROLE_NOT_READY_MARKERS = ("unable to assume the role",)
ROLE_RETRY_ATTEMPTS = 6
ROLE_RETRY_DELAY_SECONDS = 10


class AwsApi:
    def __init__(self, foundation: FoundationOutputs, runner: Optional[Runner] = None) -> None:
        self.foundation = foundation
        self.runner = runner or Runner()
        self._sleep = time.sleep

    def state_exists(self, bucket: str, key: str) -> bool:
        """True when the app Terraform state object exists. Missing is False; other errors raise."""
        result = self.runner.run(
            ["aws", "s3api", "head-object", "--bucket", bucket, "--key", key,
             "--region", self.foundation.region, "--no-cli-pager"],
            check=False,
        )
        if result.returncode == 0:
            return True
        text = (result.stderr or result.stdout or "").strip()
        if "Not Found" in text or "(404)" in text or "NoSuchKey" in text:
            return False
        raise CommandError("could not check Terraform state {}: {}".format(key, text[-500:]))

    def live_services(self, service_names: Iterable[str]) -> Dict[str, int]:
        """ECS services of this app that are ACTIVE with desired or running tasks."""
        names = sorted(set(service_names))
        live: Dict[str, int] = {}
        for start in range(0, len(names), 10):
            value = self.runner.json(
                self._base("ecs", "describe-services")
                + ["--cluster", self.foundation.ecs_cluster_arn, "--services", *names[start:start + 10],
                   "--output", "json"]
            )
            for service in value.get("services", []):
                desired = int(service.get("desiredCount", 0) or 0)
                running = int(service.get("runningCount", 0) or 0)
                if service.get("status") == "ACTIVE" and (desired > 0 or running > 0):
                    live[str(service.get("serviceName"))] = max(desired, running)
        return live

    def _base(self, service: str, operation: str) -> List[str]:
        return ["aws", service, operation, "--region", self.foundation.region, "--no-cli-pager"]

    def verify_caller(self) -> None:
        value = self.runner.json(["aws", "sts", "get-caller-identity", "--no-cli-pager"])
        account = value.get("Account") if isinstance(value, dict) else None
        if account != self.foundation.account_id:
            raise ContractError(
                "AWS caller account {} does not match Foundation {}".format(account, self.foundation.account_id)
            )

    def ensure_listener_priority(self, priority: int, hostname: str) -> None:
        value = self.runner.json(
            self._base("elbv2", "describe-rules")
            + ["--listener-arn", self.foundation.https_listener_arn, "--output", "json"]
        )
        for rule in value.get("Rules", []):
            if str(rule.get("Priority")) != str(priority):
                continue
            hosts = []
            for condition in rule.get("Conditions", []):
                if condition.get("Field") == "host-header":
                    hosts.extend(condition.get("Values", []))
                    hosts.extend(condition.get("HostHeaderConfig", {}).get("Values", []))
            if hostname not in hosts:
                raise ContractError(
                    "ALB listener priority {} is already owned by another hostname".format(priority)
                )

    def put_secret(self, secret_arn: str, secret_value: Mapping[str, Any]) -> None:
        payload = json.dumps(secret_value, separators=(",", ":"))
        self.runner.run(
            self._base("secretsmanager", "put-secret-value")
            + ["--secret-id", secret_arn, "--secret-string", "file:///dev/stdin"],
            input_text=payload,
            sensitive=True,
        )

    def current_secret_version(self, secret_arn: str) -> str:
        # Metadata only; the host never retrieves the database password.
        value = self.runner.json(self._base("secretsmanager", "describe-secret")
                                 + ["--secret-id", secret_arn, "--output", "json"])
        current = [version for version, stages in value.get("VersionIdsToStages", {}).items()
                   if "AWSCURRENT" in stages]
        if len(current) != 1:
            raise DeploymentError("database secret has no unique current version")
        return current[0]

    def run_task(
        self,
        task_definition: str,
        security_group_id: str,
        private_subnet_ids: Sequence[str],
        label: str,
        timeout_seconds: int,
        log_group: Optional[str] = None,
        log_prefix: Optional[str] = None,
    ) -> str:
        network = {
            "awsvpcConfiguration": {
                "subnets": list(private_subnet_ids),
                "securityGroups": [security_group_id],
                "assignPublicIp": "DISABLED",
            }
        }
        command = self._base("ecs", "run-task") + [
            "--cluster", self.foundation.ecs_cluster_arn,
            "--launch-type", "FARGATE",
            "--task-definition", task_definition,
            "--network-configuration", json.dumps(network, separators=(",", ":")),
            "--count", "1",
            "--output", "json",
        ]
        attempt = 0
        while True:
            attempt += 1
            try:
                value = self.runner.json(command)
                break
            except CommandError as exc:
                role_not_ready = any(marker in str(exc) for marker in ROLE_NOT_READY_MARKERS)
                if not role_not_ready or attempt >= ROLE_RETRY_ATTEMPTS:
                    raise
                self._sleep(ROLE_RETRY_DELAY_SECONDS)
        failures = value.get("failures", [])
        tasks = value.get("tasks", [])
        if failures or len(tasks) != 1:
            raise DeploymentError("{} task did not start: {}".format(label, failures))
        task_arn = tasks[0].get("taskArn")
        if not task_arn:
            raise DeploymentError("{} task response omitted taskArn".format(label))
        self._wait_task(task_arn, label, timeout_seconds, log_group, log_prefix)
        return task_arn

    def _wait_task(
        self,
        task_arn: str,
        label: str,
        timeout_seconds: int,
        log_group: Optional[str] = None,
        log_prefix: Optional[str] = None,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        last_status = "UNKNOWN"
        while time.monotonic() < deadline:
            value = self.runner.json(
                self._base("ecs", "describe-tasks")
                + ["--cluster", self.foundation.ecs_cluster_arn, "--tasks", task_arn, "--output", "json"]
            )
            failures = value.get("failures", [])
            tasks = value.get("tasks", [])
            if failures or len(tasks) != 1:
                raise DeploymentError("{} task lookup failed: {}".format(label, failures))
            task = tasks[0]
            last_status = task.get("lastStatus", "UNKNOWN")
            if last_status == "STOPPED":
                containers = task.get("containers", [])
                failed = [
                    item for item in containers
                    if item.get("exitCode") != 0 or item.get("reason")
                ]
                if task.get("stopCode") == "TaskFailedToStart" or failed:
                    raise DeploymentError(
                        _task_failure_message(label, task_arn, task, failed or containers, log_group, log_prefix)
                    )
                return
            time.sleep(5)
        raise DeploymentError("{} task timed out in status {}".format(label, last_status))

    def wait_services(self, service_names: Iterable[str], timeout_seconds: int) -> None:
        names = list(service_names)
        if not names:
            return
        deadline = time.monotonic() + timeout_seconds
        last_detail = ""
        while time.monotonic() < deadline:
            value = self.runner.json(
                self._base("ecs", "describe-services")
                + ["--cluster", self.foundation.ecs_cluster_arn, "--services", *names, "--output", "json"]
            )
            failures = value.get("failures", [])
            if failures:
                raise DeploymentError("ECS service lookup failed: {}".format(failures))
            services = value.get("services", [])
            stable = True
            details = []
            for service in services:
                deployments = service.get("deployments", [])
                desired = service.get("desiredCount", 0)
                running = service.get("runningCount", 0)
                pending = service.get("pendingCount", 0)
                details.append("{} desired={} running={} pending={}".format(service.get("serviceName"), desired, running, pending))
                if len(deployments) != 1 or running != desired or pending != 0:
                    stable = False
                rollout = deployments[0].get("rolloutState") if deployments else None
                if rollout == "FAILED":
                    reason = deployments[0].get("rolloutStateReason", "deployment circuit breaker")
                    raise DeploymentError("ECS deployment failed: {}".format(reason))
            last_detail = "; ".join(details)
            if stable and len(services) == len(names):
                return
            time.sleep(10)
        raise DeploymentError("ECS services did not stabilize: {}".format(last_detail))

    def wait_target_healthy(self, target_group_arn: str, timeout_seconds: int) -> None:
        deadline = time.monotonic() + timeout_seconds
        last_detail = "no targets"
        while time.monotonic() < deadline:
            value = self.runner.json(
                self._base("elbv2", "describe-target-health")
                + ["--target-group-arn", target_group_arn, "--output", "json"]
            )
            descriptions = value.get("TargetHealthDescriptions", [])
            states = [item.get("TargetHealth", {}).get("State") for item in descriptions]
            reasons = [item.get("TargetHealth", {}).get("Reason", "") for item in descriptions]
            last_detail = "states={} reasons={}".format(states, reasons)
            if descriptions and all(state == "healthy" for state in states):
                return
            time.sleep(10)
        raise DeploymentError("target group did not become healthy: {}".format(last_detail))


def _task_failure_message(
    label: str,
    task_arn: str,
    task: Mapping[str, Any],
    containers: Sequence[Mapping[str, Any]],
    log_group: Optional[str],
    log_prefix: Optional[str],
) -> str:
    """Failure reason with the exact CloudWatch log location so the cause can be read directly."""
    reasons = []
    for item in containers:
        text = "{}: exit={} reason={}".format(item.get("name", "container"), item.get("exitCode"), item.get("reason", ""))
        if item.get("exitCode") == 255:
            text += " (exit 255 often means the image cannot run on X86_64; check the linux/amd64 build)"
        reasons.append(text)
    message = "{} task failed: {}".format(label, "; ".join(reasons))
    stopped = task.get("stoppedReason")
    if stopped:
        message += " | stopped: {}".format(stopped)
    if log_group and log_prefix:
        task_id = task_arn.rsplit("/", 1)[-1]
        streams = ["{}/{}/{}".format(log_prefix, item.get("name", "container"), task_id) for item in containers]
        message += " | logs: {} {}".format(log_group, ", ".join(streams))
    return message
