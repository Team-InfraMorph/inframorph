from typing import Literal
from pydantic import model_validator

from .common import SCHEMA_VERSION, ContractModel, Revision, Target

class BuildArtifact(ContractModel):
    schema_version: Literal["1.0.0"] = SCHEMA_VERSION
    source_revision: Revision
    target: Target
    image: str
    platform: Literal["linux/amd64"]

    @model_validator(mode="after")
    def check_image(self):
        if self.image != f"app:{self.source_revision}":
            raise ValueError("Builder 이미지 태그와 원본 커밋이 다릅니다")
        return self