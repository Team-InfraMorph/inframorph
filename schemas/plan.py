from typing import Literal
from pydantic import Field, model_validator

from .common import SCHEMA_VERSION, ContractModel, EnvName, Name, RelativePath, Revision, Target, WorkloadKind

class ServiceSpec(ContractModel):
    name: Name
    kind: WorkloadKind
    cpu: int = Field(gt=0)
    mem: int = Field(gt=0)
    port: int | None = Field(default=None, ge=1, le=65535)
    health: str | None = None
    public: bool = False
    command: str | None = None

    @model_validator(mode="after")
    def check_kind_fields(self):
        if self.kind == WorkloadKind.HTTP:
            if self.port is None or not self.health or not self.health.startswith("/"):
                raise ValueError("http service에는 port와 health가 필요합니다")
        else:
            if not self.command:
                raise ValueError("worker service에는 command가 필요합니다")
            if self.public or self.port is not None or self.health is not None:
                raise ValueError("worker는 비공개이며 port/health가 없어야 합니다")
        return self

class DbPlan(ContractModel):
    type: Literal["postgres_container", "rds_postgres"]
    patch: Literal["sqlite_to_postgres"]

class StoragePlan(ContractModel):
    type: Literal["volume", "s3"]
    patch: Literal["fs_to_storage"]
    path: RelativePath

class Plan(ContractModel):
    schema_version: Literal["1.0.0"] = SCHEMA_VERSION
    source_revision: Revision
    target: Target
    app: Name
    image_tag: str
    services: list[ServiceSpec] = Field(min_length=1)
    db: DbPlan | None = None
    storage: StoragePlan | None = None
    secrets: list[EnvName] = Field(default_factory=list)
    config: dict[EnvName, str] = Field(default_factory=dict)
    logs: Literal["docker", "cloudwatch"]
    est_monthly_krw: int | None = Field(default=None, ge=0)
    mermaid: str = Field(min_length=1)

    @model_validator(mode="after")
    def check_plan(self):
        if self.image_tag != f"app:{self.source_revision}":
            raise ValueError("image_tag는 app:<source_revision>이어야 합니다")

        names = [service.name for service in self.services]
        if len(names) != len(set(names)):
            raise ValueError("service 이름이 중복되었습니다")

        public_http = [
            service for service in self.services
            if service.kind == WorkloadKind.HTTP and service.public
        ]
        if len(public_http) != 1:
            raise ValueError("이번 MVP는 공개 http service 하나가 필요합니다")

        if self.db is not None and "DATABASE_URL" not in self.secrets:
            raise ValueError("DB를 쓰는 Plan에는 DATABASE_URL 이름이 필요합니다")

        if self.target in (Target.LOCAL, Target.ONPREM):  # 사내 서버도 같은 Docker 구성
            if self.logs != "docker":
                raise ValueError("Local 로그는 docker입니다")
            if self.db is not None and self.db.type != "postgres_container":
                raise ValueError("Local DB는 postgres_container입니다")
            if self.storage is not None:
                if self.storage.type != "volume":
                    raise ValueError("Local 저장소는 volume입니다")
                if self.config.get("STORAGE_DRIVER") != "fs":
                    raise ValueError("Local STORAGE_DRIVER는 fs입니다")
        else:
            if self.logs != "cloudwatch":
                raise ValueError("AWS 로그는 cloudwatch입니다")
            if self.db is not None and self.db.type != "rds_postgres":
                raise ValueError("AWS DB는 rds_postgres입니다")
            if self.storage is not None:
                if self.storage.type != "s3":
                    raise ValueError("AWS 저장소는 s3입니다")
                if self.config.get("STORAGE_DRIVER") != "s3":
                    raise ValueError("AWS STORAGE_DRIVER는 s3입니다")
        return self
