from typing import Literal
from pydantic import Field, model_validator

from .common import SCHEMA_VERSION, ContractModel, EnvName, Evidence, Name, RelativePath, Revision, WorkloadKind, DbEngine

class Workload(ContractModel):
    name: Name
    kind: WorkloadKind
    port: int | None = Field(default=None, ge=1, le=65535)
    public: bool = False
    health: str | None = None
    command: str | None = None
    evidence: list[Evidence] = Field(min_length=1)

    @model_validator(mode="after")
    def check_kind_fields(self):
        if self.kind == WorkloadKind.HTTP:
            if self.port is None or not self.health or not self.health.startswith("/"):
                raise ValueError("http workload에는 port와 /로 시작하는 health가 필요합니다")
            if self.command is not None:
                raise ValueError("MVP의 http 실행 명령은 이미지 CMD에서 결정합니다")
        else:
            if not self.command:
                raise ValueError("worker에는 실행 command가 필요합니다")
            if self.public or self.port is not None or self.health is not None:
                raise ValueError("worker는 비공개이며 port/health를 갖지 않습니다")
        return self

class StateItem(ContractModel):
    kind: Literal["relational_db", "persistent_files"]
    engine: DbEngine | None = None
    orm: Literal["prisma"] | None = None
    path: RelativePath | None = None
    reason: str | None = None
    evidence: list[Evidence] = Field(min_length=1)

    @model_validator(mode="after")
    def check_kind_fields(self):
        if self.kind == "relational_db":
            if self.engine is None or self.orm is None:
                raise ValueError("relational_db에는 engine과 orm이 필요합니다")
            if self.path is not None or self.reason is not None:
                raise ValueError("relational_db에는 파일 경로를 넣지 않습니다")
        else:
            if self.path is None or not self.reason:
                raise ValueError("persistent_files에는 path와 reason이 필요합니다")
            if self.engine is not None or self.orm is not None:
                raise ValueError("persistent_files에는 DB 필드를 넣지 않습니다")
        return self

class Intent(ContractModel):
    schema_version: Literal["1.0.0"] = SCHEMA_VERSION
    source_revision: Revision
    app: Name
    runtime: Literal["node22"]
    workloads: list[Workload] = Field(min_length=1)
    state: list[StateItem] = Field(default_factory=list)
    secrets: list[EnvName] = Field(default_factory=list)
    config: dict[EnvName, str] = Field(default_factory=dict)
    unknowns: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_workloads(self):
        names = [item.name for item in self.workloads]
        if len(names) != len(set(names)):
            raise ValueError("workload 이름이 중복되었습니다")
        public_http = [
            item for item in self.workloads
            if item.kind == WorkloadKind.HTTP and item.public
        ]
        if len(public_http) != 1:
            raise ValueError("이번 MVP는 공개 http workload 하나가 필요합니다")
        return self
