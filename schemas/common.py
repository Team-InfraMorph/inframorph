import re
from enum import Enum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict

SCHEMA_VERSION = "1.0.0"

class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

class Target(str, Enum):
    LOCAL = "local"
    AWS = "aws"
    GCP = "gcp"
    ONPREM = "onprem"  # Local 테스트를 통과한 같은 이미지를 사내 서버(원격 Docker)에 배포

class WorkloadKind(str, Enum):
    HTTP = "http"
    WORKER = "worker"

def check_revision(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ValueError("source revision은 소문자 전체 40자리 Git SHA여야 합니다")
    return value

def check_name(value: str) -> str:
    if re.fullmatch(r"[a-z][a-z0-9-]{0,40}", value) is None:
        raise ValueError("이름은 소문자로 시작하고 소문자, 숫자, -만 사용해야 합니다")
    return value

def check_env_name(value: str) -> str:
    if re.fullmatch(r"[A-Z][A-Z0-9_]*", value) is None:
        raise ValueError("환경 변수 이름은 대문자·숫자·_만 사용해야 합니다")
    return value

def check_relative_path(value: str) -> str:
    parts = value.rstrip("/").split("/")
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError("저장소 기준 상대 경로만 허용합니다")
    return value

def parse_evidence(value: str) -> tuple[str, int]:
    match = re.fullmatch(r"([A-Za-z0-9._/-]+):([1-9][0-9]*)", value)
    if match is None:
        raise ValueError("근거는 파일:줄번호 형식이어야 합니다")
    path = check_relative_path(match.group(1))
    return path, int(match.group(2))

def check_evidence(value: str) -> str:
    parse_evidence(value)
    return value

Revision = Annotated[str, AfterValidator(check_revision)]
Name = Annotated[str, AfterValidator(check_name)]
EnvName = Annotated[str, AfterValidator(check_env_name)]
RelativePath = Annotated[str, AfterValidator(check_relative_path)]
Evidence = Annotated[str, AfterValidator(check_evidence)]
