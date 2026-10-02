"""Explicit host configuration. No AWS credentials are accepted from the model or UI."""
import json
import os
from pathlib import Path
import re

from pydantic import Field
from schemas.common import ContractModel
from adapters.aws.contracts import FoundationOutputs


class AwsConfig(ContractModel):
    foundation: str
    account_id: str = Field(pattern=r"^[0-9]{12}$")
    region: str = "ap-northeast-2"
    profile: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    credentials_file: str
    config_file: str
    tools_dir: str
    state_bucket: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
    migration_command: str = "./node_modules/.bin/prisma db push --skip-generate"
    timeout_seconds: int = Field(default=1800, ge=60, le=3600)


def owned_file(path):
    path = Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("aws_config_path_invalid")
    stat = path.stat()
    if not path.is_file() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise ValueError("aws_config_not_private")
    return path


def load_config(path):
    raw = owned_file(path).read_bytes()
    if len(raw) > 16_000:
        raise ValueError("aws_config_limit")
    config = AwsConfig.model_validate_json(raw)
    for name in (config.foundation, config.credentials_file, config.config_file):
        owned_file(name)
    tools = Path(config.tools_dir)
    if not tools.is_absolute() or any(p.is_symlink() for p in (tools, *tools.parents)):
        raise ValueError("aws_tools_invalid")
    if not all((tools / name).is_file() for name in ("aws", "terraform")):
        raise ValueError("aws_tools_unavailable")
    # This runtime is limited to reviewed demo code without existing migrations.
    if config.migration_command != "./node_modules/.bin/prisma db push --skip-generate":
        raise ValueError("aws_migration_not_reviewed")
    FoundationOutputs.from_file(Path(config.foundation), config.account_id, config.region)
    return config


def app_name(project_id):
    if not re.fullmatch(r"p-[0-9a-f]{12}", project_id):
        raise ValueError("aws_project_binding_invalid")
    return "cp-" + project_id


def environment(config, base):
    # base is already sanitized. Add AWS only to the dedicated deployment worker.
    cache = Path(config.tools_dir).parent / "terraform-plugin-cache"
    if cache.is_symlink():
        raise ValueError("aws_tools_invalid")
    return dict(base) | {
        "AWS_PROFILE": config.profile,
        "AWS_SHARED_CREDENTIALS_FILE": config.credentials_file,
        "AWS_CONFIG_FILE": config.config_file,
        "AWS_REGION": config.region,
        "AWS_DEFAULT_REGION": config.region,
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_PAGER": "",
        "PATH": config.tools_dir + os.pathsep + base.get("PATH", ""),
        "TF_IN_AUTOMATION": "1",
        "TF_PLUGIN_CACHE_DIR": str(cache),
    }
