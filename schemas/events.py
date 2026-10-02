from datetime import datetime, timezone
from typing import Literal
from pydantic import Field, field_validator

from .common import ContractModel, Target

class DeployEvent(ContractModel):
    deployment_id: str = Field(min_length=1)
    ts: datetime
    target: Target
    step: Literal[
        "snapshot", "map", "analyze", "policy", "plan", "patch",
        "build", "push", "infra", "start", "health", "url",
        "smoke", "rollback",
    ]
    status: Literal["started", "ok", "fail"]
    detail: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    url: str | None = None

    @field_validator("ts")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("ts에는 시간대가 필요합니다")
        return value.astimezone(timezone.utc)

    def jsonl(self) -> str:
        return self.model_dump_json(exclude_none=True) + "\n"