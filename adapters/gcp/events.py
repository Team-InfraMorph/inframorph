import json
import sys
from datetime import datetime, timezone
from typing import Optional, TextIO

from .errors import ContractError


STEPS = {
    "snapshot", "map", "analyze", "policy", "plan", "patch", "build",
    "push", "infra", "start", "health", "url", "smoke", "rollback",
}
STATUSES = {"started", "ok", "fail"}


class EventEmitter:
    """Write only DeployEvent v1 JSONL to the configured stream.

    ``target`` is ``gcp``; the shared schema gains that value when the Control
    Plane integration lands.
    """

    def __init__(self, deployment_id: str, stream: Optional[TextIO] = None) -> None:
        if not deployment_id:
            raise ContractError("deployment_id is required")
        self.deployment_id = deployment_id
        self.stream = stream or sys.stdout

    def emit(
        self,
        step: str,
        status: str,
        detail: Optional[str] = None,
        duration_ms: Optional[int] = None,
        url: Optional[str] = None,
    ) -> None:
        if step not in STEPS or status not in STATUSES:
            raise ContractError("invalid DeployEvent step or status")
        event = {
            "deployment_id": self.deployment_id,
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "target": "gcp",
            "step": step,
            "status": status,
        }
        if detail is not None:
            event["detail"] = detail[:4000]
        if duration_ms is not None:
            if duration_ms < 0:
                raise ContractError("DeployEvent duration_ms cannot be negative")
            event["duration_ms"] = duration_ms
        if url is not None:
            event["url"] = url
        self.stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.stream.flush()
