"""사내 서버(온프레미스) 연결 설정. 레포·모델·화면에서 받지 않고 운영자의 비공개 파일에서만 읽는다."""
import ipaddress
import os
from pathlib import Path

from pydantic import Field, field_validator
from schemas.common import ContractModel


class OnpremConfig(ContractModel):
    docker_host: str = Field(pattern=r"^ssh://[A-Za-z0-9._-]{1,64}@[A-Za-z0-9.-]{1,253}$")  # 원격 Docker (SSH)
    host: str = Field(pattern=r"^[A-Za-z0-9.-]{1,253}$")  # 조종실이 앱에 접속할 주소 (예: Tailscale IP)
    bind: str = "0.0.0.0"  # 사내 서버에서 앱 포트를 열 주소
    label: str = Field(default="사내 서버", max_length=40)
    timeout_seconds: int = Field(default=600, ge=60, le=1800)

    @field_validator("bind")
    @classmethod
    def check_bind(cls, value):
        ipaddress.ip_address(value)
        return value


def load_config(path):
    path = Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("onprem_config_path_invalid")
    info = path.stat()
    if not path.is_file() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("onprem_config_not_private")
    raw = path.read_bytes()
    if len(raw) > 4_000:
        raise ValueError("onprem_config_limit")
    return OnpremConfig.model_validate_json(raw)
