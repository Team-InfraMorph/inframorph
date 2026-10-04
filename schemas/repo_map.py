from typing import Literal
from pydantic import Field

from .common import SCHEMA_VERSION, ContractModel, Evidence, Revision, DbEngine

class Hint(ContractModel):
    type: Literal["file_write", "env", "process"]
    at: Evidence
    name: str | None = None

class DbInfo(ContractModel):
    orm: Literal["prisma"] | None = None
    provider: DbEngine | None = None

class RepoMap(ContractModel):
    schema_version: Literal["1.0.0"] = SCHEMA_VERSION
    commit: Revision
    tree: list[str] = Field(min_length=1)
    deps: list[str]
    db: DbInfo | None = None
    entrypoints: dict[str, str]
    routes: list[str]
    hints: list[Hint]