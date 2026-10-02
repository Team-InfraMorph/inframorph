import json
from dataclasses import asdict, dataclass
from typing import Mapping, Optional

from .errors import ContractError
from .records import DeploymentRecord

FIRST = "first"
REDEPLOY = "redeploy"
RESUME = "resume"


@dataclass(frozen=True)
class DeployMode:
    """How this deployment relates to what already exists for the same Plan.app."""

    mode: str
    reason: str
    previous_deployment_id: Optional[str] = None
    previous_source_revision: Optional[str] = None

    @property
    def new_url(self) -> bool:
        return self.mode != REDEPLOY

    def detail(self, **extra: str) -> str:
        value = {key: item for key, item in asdict(self).items() if item is not None}
        value["url"] = "new" if self.new_url else "same"
        value.update(extra)
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def detect_mode(
    app_id: str,
    previous: Optional[DeploymentRecord],
    state_exists: bool,
    live_services: Mapping[str, int],
    state_key: str,
) -> DeployMode:
    """Compare the local deployment record with the real AWS state before changing anything.

    first    : no record, no Terraform state, no running service -> new app and new URL
    resume   : no record, Terraform state from an unfinished earlier attempt, nothing running
    redeploy : record of the last successful deployment and its Terraform state -> same URL
    Anything else is inconsistent and stops before any AWS change.
    """
    if previous is not None:
        if not state_exists:
            raise ContractError(
                "inconsistent state for {}: a deployment record exists but Terraform state {} "
                "is missing; refusing to recreate a live app from scratch".format(app_id, state_key)
            )
        return DeployMode(
            REDEPLOY,
            "previous successful deployment found; the same URL is updated",
            previous.deployment_id,
            previous.source_revision,
        )
    if live_services:
        raise ContractError(
            "inconsistent state for {}: services are running ({}) but no deployment record was "
            "provided; pass the record of the last successful deployment".format(
                app_id, ", ".join(sorted(live_services))
            )
        )
    if state_exists:
        return DeployMode(
            RESUME,
            "Terraform state exists from an earlier unfinished attempt and no service is running; "
            "continuing as a first deploy",
        )
    return DeployMode(FIRST, "no record, no Terraform state and no running service; a new app and URL are created")
