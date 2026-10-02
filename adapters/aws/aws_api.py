import json
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .contracts import FoundationOutputs
from .errors import ContractError, DeploymentError
from .process import Runner


class AwsApi:
    def __init__(self, foundation: FoundationOutputs, runner: Optional[Runner] = None) -> None:
        self.foundation = foundation
        self.runner = runner or Runner()

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

    def run_task(
        self,
        task_definition: str,
        security_group_id: str,
        private_subnet_ids: Sequence[str],
        label: str,
        timeout_seconds: int,
    ) -> str:
        network = {
            "awsvpcConfiguration": {
                "subnets": list(private_subnet_ids),
                "securityGroups": [security_group_id],
                "assignPublicIp": "DISABLED",
            }
        }
        value = self.runner.json(
            self._base("ecs", "run-task")
            + [
                "--cluster", self.foundation.ecs_cluster_arn,
                "--launch-type", "FARGATE",
                "--task-definition", task_definition,
                "--network-configuration", json.dumps(network, separators=(",", ":")),
                "--count", "1",
                "--output", "json",
            ]
        )
        failures = value.get("failures", [])
        tasks = value.get("tasks", [])
        if failures or len(tasks) != 1:
            raise DeploymentError("{} task did not start: {}".format(label, failures))
        task_arn = tasks[0].get("taskArn")
        if not task_arn:
            raise DeploymentError("{} task response omitted taskArn".format(label))
        self._wait_task(task_arn, label, timeout_seconds)
        return task_arn

    def _wait_task(self, task_arn: str, label: str, timeout_seconds: int) -> None:
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
                    reasons = [
                        "{}: exit={} reason={}".format(
                            item.get("name", "container"), item.get("exitCode"), item.get("reason", "")
                        )
                        for item in failed
                    ]
                    raise DeploymentError("{} task failed: {}".format(label, "; ".join(reasons)))
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
