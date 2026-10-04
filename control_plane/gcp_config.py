"""Explicit host configuration for the trusted GCP deployment worker."""
import os
from pathlib import Path
import re

from pydantic import Field

from schemas.common import ContractModel
from adapters.gcp.contracts import FoundationOutputs


class GcpConfig(ContractModel):
    foundation: str
    project_id: str = Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
    region: str = "asia-northeast3"
    gcloud_config_dir: str
    tools_dir: str
    state_bucket: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$")
    migration_command: str = "./node_modules/.bin/prisma db push --skip-generate"
    timeout_seconds: int = Field(default=1800, ge=60, le=3600)


def _absolute(path, code):
    value = Path(path)
    if not value.is_absolute() or any(part.is_symlink() for part in (value, *value.parents)):
        raise ValueError(code)
    return value


def owned_file(path):
    value = _absolute(path, "gcp_config_path_invalid")
    stat = value.stat()
    if not value.is_file() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise ValueError("gcp_config_not_private")
    return value


def owned_directory(path):
    value = _absolute(path, "gcp_config_path_invalid")
    stat = value.stat()
    if not value.is_dir() or stat.st_uid != os.getuid() or stat.st_mode & 0o022:
        raise ValueError("gcp_config_not_private")
    return value


def load_config(path):
    raw = owned_file(path).read_bytes()
    if len(raw) > 16_000:
        raise ValueError("gcp_config_limit")
    config = GcpConfig.model_validate_json(raw)
    owned_file(config.foundation)
    owned_directory(config.gcloud_config_dir)
    tools = _absolute(config.tools_dir, "gcp_tools_invalid")
    if not all((tools / name).is_file() for name in ("gcloud", "terraform")):
        raise ValueError("gcp_tools_unavailable")
    if config.migration_command != "./node_modules/.bin/prisma db push --skip-generate":
        raise ValueError("gcp_migration_not_reviewed")
    FoundationOutputs.from_file(Path(config.foundation), config.project_id, config.region)
    return config


def app_name(project_id):
    if not re.fullmatch(r"p-[0-9a-f]{12}", project_id):
        raise ValueError("gcp_project_binding_invalid")
    return "cp-" + project_id


def environment(config, base):
    """Add only operator-selected GCP identity settings to a sanitized environment."""
    foundation = FoundationOutputs.from_file(Path(config.foundation), config.project_id, config.region)
    cache = Path(config.tools_dir).parent / "terraform-plugin-cache"
    if cache.is_symlink():
        raise ValueError("gcp_tools_invalid")
    return dict(base) | {
        "CLOUDSDK_CONFIG": config.gcloud_config_dir,
        "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT": foundation.deployer_service_account_email,
        "CLOUDSDK_CORE_PROJECT": config.project_id,
        "CLOUDSDK_CORE_DISABLE_PROMPTS": "1",
        "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT": foundation.deployer_service_account_email,
        "GOOGLE_PROJECT": config.project_id,
        "GCP_REGION": config.region,
        "PATH": config.tools_dir + os.pathsep + base.get("PATH", ""),
        "TF_IN_AUTOMATION": "1",
        "TF_PLUGIN_CACHE_DIR": str(cache),
    }
