"""Strict validation of the deployment contracts for the GCP target.

The Plan and BuildArtifact wire format is the shared v1.0.0 contract also used
by the AWS Adapter; only the target-specific values differ (Cloud SQL, Cloud
Storage, Cloud Logging). This validator is the Adapter's security boundary even
after the shared schemas learn the ``gcp`` target.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from .errors import ContractError
from schemas.plan import DbPlan as SharedDbPlan


SCHEMA_VERSION = "1.0.0"
REGION = "asia-northeast3"
APP_RESOURCE_PREFIX = "im-"
REVISION_RE = re.compile(r"[0-9a-f]{40}")
NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,40}")
ENV_RE = re.compile(r"[A-Z][A-Z0-9_]*")
PROJECT_ID_RE = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
PROJECT_NUMBER_RE = re.compile(r"[0-9]{6,20}")
SERVICE_ACCOUNT_RE = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]@([a-z0-9-]+)\.iam\.gserviceaccount\.com")
IPV4_RE = re.compile(r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}")
SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")
# Cloud Run memory ceiling per whole vCPU; plan cpu is in 1/1024 vCPU units like Fargate.
CLOUD_RUN_CPU_UNITS = {256, 512, 1024, 2048, 4096, 8192}
CLOUD_RUN_MAX_MEMORY = {1: 4096, 2: 8192, 4: 16384, 8: 32768}


def _load_object(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("{}: valid JSON object is required: {}".format(path, exc)) from exc
    if not isinstance(value, dict):
        raise ContractError("{}: top-level JSON value must be an object".format(path))
    return value


def _keys(value: Mapping[str, Any], label: str, required: Set[str], optional: Set[str]) -> None:
    actual = set(value)
    missing = required - actual
    extra = actual - required - optional
    if missing:
        raise ContractError("{}: missing fields: {}".format(label, ", ".join(sorted(missing))))
    if extra:
        raise ContractError("{}: unknown fields: {}".format(label, ", ".join(sorted(extra))))


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError("{} must be a non-empty string".format(label))
    return value


def _optional_string(value: Any, label: str) -> Optional[str]:
    if value is None:
        return None
    return _string(value, label)


def _bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError("{} must be a boolean".format(label))
    return value


def _int(value: Any, label: str, minimum: int = 0, maximum: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ContractError("{} must be an integer >= {}".format(label, minimum))
    if maximum is not None and value > maximum:
        raise ContractError("{} must be <= {}".format(label, maximum))
    return value


def _name(value: Any, label: str) -> str:
    result = _string(value, label)
    if NAME_RE.fullmatch(result) is None or result.endswith("-"):
        raise ContractError(
            "{} must start with a lowercase letter, end with a letter or digit and contain at most "
            "41 lowercase letters, digits, or hyphens".format(label)
        )
    return result


def _revision(value: Any, label: str) -> str:
    result = _string(value, label)
    if REVISION_RE.fullmatch(result) is None:
        raise ContractError("{} must be a lowercase full 40-character Git SHA".format(label))
    return result


def _env_names(value: Any, label: str) -> Tuple[str, ...]:
    if not isinstance(value, list):
        raise ContractError("{} must be an array".format(label))
    result: List[str] = []
    for index, item in enumerate(value):
        name = _string(item, "{}[{}]".format(label, index))
        if ENV_RE.fullmatch(name) is None:
            raise ContractError("{}[{}] is not an environment variable name".format(label, index))
        result.append(name)
    if len(result) != len(set(result)):
        raise ContractError("{} contains duplicate names".format(label))
    return tuple(result)


def _config(value: Any, label: str) -> Dict[str, str]:
    if not isinstance(value, dict):
        raise ContractError("{} must be an object".format(label))
    result: Dict[str, str] = {}
    for key, item in value.items():
        if ENV_RE.fullmatch(key) is None or not isinstance(item, str):
            raise ContractError("{} values must map environment names to strings".format(label))
        result[key] = item
    return result


def cloud_run_vcpu(cpu_units: int) -> int:
    """Whole vCPUs Cloud Run runs a service with (the Terraform module rounds up the same way)."""
    return max(1, -(-cpu_units // 1024))


def _valid_cloud_run_size(cpu: int, memory: int) -> bool:
    if cpu not in CLOUD_RUN_CPU_UNITS:
        return False
    return 128 <= memory <= CLOUD_RUN_MAX_MEMORY[cloud_run_vcpu(cpu)]


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    kind: str
    cpu: int
    memory: int
    port: Optional[int]
    health: Optional[str]
    public: bool
    command: Optional[str]

    @classmethod
    def parse(cls, value: Any, index: int) -> "ServiceSpec":
        label = "plan.services[{}]".format(index)
        if not isinstance(value, dict):
            raise ContractError("{} must be an object".format(label))
        _keys(value, label, {"name", "kind", "cpu", "mem"}, {"port", "health", "public", "command"})
        name = _name(value["name"], label + ".name")
        kind = _string(value["kind"], label + ".kind")
        if kind not in {"http", "worker"}:
            raise ContractError("{}.kind must be http or worker".format(label))
        cpu = _int(value["cpu"], label + ".cpu", 1)
        memory = _int(value["mem"], label + ".mem", 1)
        if not _valid_cloud_run_size(cpu, memory):
            raise ContractError("{} has an unsupported Cloud Run cpu/memory combination".format(label))
        port_value = value.get("port")
        port = None if port_value is None else _int(port_value, label + ".port", 1, 65535)
        health = _optional_string(value.get("health"), label + ".health")
        public = _bool(value.get("public", False), label + ".public")
        command = _optional_string(value.get("command"), label + ".command")
        if kind == "http":
            if port is None or health is None or not health.startswith("/"):
                raise ContractError("{} http service requires port and an absolute health path".format(label))
        else:
            if not command:
                raise ContractError("{} worker service requires command".format(label))
            if public or port is not None or health is not None:
                raise ContractError("{} worker must be private and have no port or health path".format(label))
        return cls(name, kind, cpu, memory, port, health, public, command)


@dataclass(frozen=True)
class DbPlan:
    type: str
    patch: str
    source_engine: str
    target_engine: str
    orm: str


@dataclass(frozen=True)
class StoragePlan:
    type: str
    patch: str
    path: str


@dataclass(frozen=True)
class Plan:
    schema_version: str
    source_revision: str
    app: str
    image_tag: str
    services: Tuple[ServiceSpec, ...]
    db: Optional[DbPlan]
    storage: Optional[StoragePlan]
    secrets: Tuple[str, ...]
    config: Dict[str, str]
    logs: str
    estimated_monthly_krw: Optional[int]
    mermaid: str

    @property
    def public_service(self) -> ServiceSpec:
        return next(item for item in self.services if item.public)

    @classmethod
    def from_file(cls, path: Path) -> "Plan":
        return cls.parse(_load_object(path))

    @classmethod
    def parse(cls, value: Mapping[str, Any]) -> "Plan":
        _keys(
            value,
            "plan",
            {"source_revision", "target", "app", "image_tag", "services", "logs", "mermaid"},
            {"schema_version", "db", "storage", "secrets", "config", "est_monthly_krw"},
        )
        version = value.get("schema_version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            raise ContractError("plan.schema_version must be {}".format(SCHEMA_VERSION))
        if value["target"] != "gcp":
            raise ContractError("plan.target must be gcp")
        source_revision = _revision(value["source_revision"], "plan.source_revision")
        app = _name(value["app"], "plan.app")
        image_tag = _string(value["image_tag"], "plan.image_tag")
        if image_tag != "app:{}".format(source_revision):
            raise ContractError("plan.image_tag must be app:<source_revision>")
        raw_services = value["services"]
        if not isinstance(raw_services, list) or not raw_services:
            raise ContractError("plan.services must be a non-empty array")
        services = tuple(ServiceSpec.parse(item, index) for index, item in enumerate(raw_services))
        names = [item.name for item in services]
        if len(names) != len(set(names)):
            raise ContractError("plan.services contains duplicate service names")
        if len([item for item in services if item.kind == "http" and item.public]) != 1:
            raise ContractError("the MVP requires exactly one public http service")

        db = None
        raw_db = value.get("db")
        if raw_db is not None:
            if not isinstance(raw_db, dict):
                raise ContractError("plan.db must be an object or null")
            # Same as AWS: the shared schema owns the source-engine conversion rules.
            try:
                checked = SharedDbPlan.model_validate(raw_db)
            except ValueError:
                raise ContractError("invalid database plan") from None
            if checked.type != "cloudsql_postgres":
                raise ContractError("GCP database must be cloudsql_postgres")
            db = DbPlan(checked.type, checked.patch, checked.source_engine, checked.target_engine, checked.orm)

        storage = None
        raw_storage = value.get("storage")
        if raw_storage is not None:
            if not isinstance(raw_storage, dict):
                raise ContractError("plan.storage must be an object or null")
            _keys(raw_storage, "plan.storage", {"type", "patch", "path"}, set())
            path = _string(raw_storage["path"], "plan.storage.path")
            if path.startswith("/") or "\\" in path or any(part in {"", ".", ".."} for part in path.rstrip("/").split("/")):
                raise ContractError("plan.storage.path must be a repository-relative path")
            if raw_storage["type"] != "gcs" or raw_storage["patch"] != "fs_to_storage":
                raise ContractError("GCP storage must be gcs with fs_to_storage patch")
            storage = StoragePlan(raw_storage["type"], raw_storage["patch"], path)

        secrets = _env_names(value.get("secrets", []), "plan.secrets")
        config = _config(value.get("config", {}), "plan.config")
        if db is not None and "DATABASE_URL" not in secrets:
            raise ContractError("a database plan requires DATABASE_URL in plan.secrets")
        if storage is not None and config.get("STORAGE_DRIVER") != "gcs":
            raise ContractError("a GCS storage plan requires STORAGE_DRIVER=gcs")
        logs = _string(value["logs"], "plan.logs")
        if logs != "cloud_logging":
            raise ContractError("GCP plan.logs must be cloud_logging")
        estimate = value.get("est_monthly_krw")
        if estimate is not None:
            estimate = _int(estimate, "plan.est_monthly_krw", 0)
        mermaid = _string(value["mermaid"], "plan.mermaid")
        return cls(version, source_revision, app, image_tag, services, db, storage, secrets, config, logs, estimate, mermaid)


@dataclass(frozen=True)
class BuildArtifact:
    schema_version: str
    source_revision: str
    image: str
    platform: str

    @classmethod
    def from_file(cls, path: Path) -> "BuildArtifact":
        return cls.parse(_load_object(path))

    @classmethod
    def parse(cls, value: Mapping[str, Any]) -> "BuildArtifact":
        _keys(value, "build_artifact", {"source_revision", "target", "image", "platform"}, {"schema_version"})
        version = value.get("schema_version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            raise ContractError("build_artifact.schema_version must be {}".format(SCHEMA_VERSION))
        if value["target"] != "gcp":
            raise ContractError("build_artifact.target must be gcp")
        revision = _revision(value["source_revision"], "build_artifact.source_revision")
        image = _string(value["image"], "build_artifact.image")
        if image != "app:{}".format(revision):
            raise ContractError("build_artifact.image must be app:<source_revision>")
        platform = _string(value["platform"], "build_artifact.platform")
        if platform != "linux/amd64":
            raise ContractError("build_artifact.platform must be linux/amd64")
        return cls(version, revision, image, platform)


FOUNDATION_REQUIRED = {
    "project_id",
    "project_number",
    "region",
    "network_id",
    "run_subnet_id",
    "artifact_registry_repository",
    "artifact_registry_url",
    "dockerhub_proxy_url",
    "cloudsql_connection_name",
    "cloudsql_private_ip",
    "cloudsql_port",
    "cloudsql_db_name",
    "cloudsql_master_username",
    "cloudsql_master_secret_id",
    "db_bootstrap_service_account_email",
    "deployer_service_account_email",
    "app_resource_prefix",
    "load_balancer_enabled",
}


def _terraform_output_values(value: Mapping[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, dict) and "value" in item and set(item).issubset({"value", "type", "sensitive"}):
            result[key] = item["value"]
        else:
            result[key] = item
    return result


def _service_account(value: Any, label: str, project_id: str) -> str:
    email = _string(value, label)
    match = SERVICE_ACCOUNT_RE.fullmatch(email)
    if match is None or match.group(1) != project_id:
        raise ContractError("{} must be a service account of project {}".format(label, project_id))
    return email


@dataclass(frozen=True)
class FoundationOutputs:
    project_id: str
    project_number: str
    region: str
    network_id: str
    run_subnet_id: str
    artifact_registry_url: str
    dockerhub_proxy_url: str
    cloudsql_connection_name: str
    cloudsql_private_ip: str
    cloudsql_port: int
    cloudsql_db_name: str
    cloudsql_master_username: str
    cloudsql_master_secret_id: str
    db_bootstrap_service_account_email: str
    deployer_service_account_email: str
    load_balancer_enabled: bool
    apps_domain: Optional[str]

    @classmethod
    def from_file(cls, path: Path, expected_project_id: str, expected_region: str = REGION) -> "FoundationOutputs":
        raw = _terraform_output_values(_load_object(path))
        missing = FOUNDATION_REQUIRED - set(raw)
        if missing:
            raise ContractError("foundation outputs missing: {}".format(", ".join(sorted(missing))))
        project_id = _string(raw["project_id"], "foundation.project_id")
        if PROJECT_ID_RE.fullmatch(project_id) is None:
            raise ContractError("foundation.project_id is invalid")
        if project_id != expected_project_id:
            raise ContractError("foundation project {} does not match {}".format(project_id, expected_project_id))
        project_number = _string(raw["project_number"], "foundation.project_number")
        if PROJECT_NUMBER_RE.fullmatch(project_number) is None:
            raise ContractError("foundation.project_number must contain digits only")
        region = _string(raw["region"], "foundation.region")
        if region != expected_region:
            raise ContractError("foundation region {} does not match {}".format(region, expected_region))
        if raw["app_resource_prefix"] != APP_RESOURCE_PREFIX:
            raise ContractError("foundation.app_resource_prefix must be {}".format(APP_RESOURCE_PREFIX))

        network_id = _string(raw["network_id"], "foundation.network_id")
        if not network_id.startswith("projects/{}/global/networks/".format(project_id)):
            raise ContractError("foundation network does not belong to the target project")
        subnet = _string(raw["run_subnet_id"], "foundation.run_subnet_id")
        if not subnet.startswith("projects/{}/regions/{}/subnetworks/".format(project_id, region)):
            raise ContractError("foundation Cloud Run subnet does not match target project and region")
        registry_prefix = "{}-docker.pkg.dev/{}/".format(region, project_id)
        registry = _string(raw["artifact_registry_url"], "foundation.artifact_registry_url")
        proxy = _string(raw["dockerhub_proxy_url"], "foundation.dockerhub_proxy_url")
        if not registry.startswith(registry_prefix) or not proxy.startswith(registry_prefix):
            raise ContractError("foundation Artifact Registry does not match target project and region")

        connection = _string(raw["cloudsql_connection_name"], "foundation.cloudsql_connection_name")
        if not connection.startswith("{}:{}:".format(project_id, region)):
            raise ContractError("foundation Cloud SQL instance does not match target project and region")
        private_ip = _string(raw["cloudsql_private_ip"], "foundation.cloudsql_private_ip")
        if IPV4_RE.fullmatch(private_ip) is None:
            raise ContractError("foundation Cloud SQL must expose a private IPv4 address")
        port = _int(raw["cloudsql_port"], "foundation.cloudsql_port", 1, 65535)
        if port != 5432:
            raise ContractError("foundation Cloud SQL port must be 5432")
        master_secret = _string(raw["cloudsql_master_secret_id"], "foundation.cloudsql_master_secret_id")
        if not re.fullmatch(r"projects/({}|{})/secrets/[A-Za-z0-9_-]+".format(project_id, project_number), master_secret):
            raise ContractError("foundation master secret does not belong to the target project")

        load_balancer = _bool(raw["load_balancer_enabled"], "foundation.load_balancer_enabled")
        apps_domain = None
        if load_balancer:
            wildcard = _string(raw.get("apps_wildcard_domain"), "foundation.apps_wildcard_domain")
            if not wildcard.startswith("*.") or wildcard.count("*") != 1:
                raise ContractError("foundation.apps_wildcard_domain must be a single-label wildcard")
            apps_domain = wildcard[2:]
        return cls(
            project_id=project_id,
            project_number=project_number,
            region=region,
            network_id=network_id,
            run_subnet_id=subnet,
            artifact_registry_url=registry,
            dockerhub_proxy_url=proxy,
            cloudsql_connection_name=connection,
            cloudsql_private_ip=private_ip,
            cloudsql_port=port,
            cloudsql_db_name=_string(raw["cloudsql_db_name"], "foundation.cloudsql_db_name"),
            cloudsql_master_username=_string(raw["cloudsql_master_username"], "foundation.cloudsql_master_username"),
            cloudsql_master_secret_id=master_secret,
            db_bootstrap_service_account_email=_service_account(
                raw["db_bootstrap_service_account_email"], "foundation.db_bootstrap_service_account_email", project_id
            ),
            deployer_service_account_email=_service_account(
                raw["deployer_service_account_email"], "foundation.deployer_service_account_email", project_id
            ),
            load_balancer_enabled=load_balancer,
            apps_domain=apps_domain,
        )


def validate_contracts(
    plan_path: Path,
    artifact_path: Path,
    foundation_path: Path,
    expected_project_id: str,
    expected_region: str = REGION,
) -> Tuple[Plan, BuildArtifact, FoundationOutputs]:
    plan = Plan.from_file(plan_path)
    artifact = BuildArtifact.from_file(artifact_path)
    foundation = FoundationOutputs.from_file(foundation_path, expected_project_id, expected_region)
    if plan.source_revision != artifact.source_revision:
        raise ContractError("Plan and BuildArtifact source_revision values do not match")
    if plan.image_tag != artifact.image:
        raise ContractError("Plan image_tag and BuildArtifact image do not match")
    return plan, artifact, foundation


def validate_digest(value: str) -> str:
    if SHA256_RE.fullmatch(value) is None:
        raise ContractError("registry digest must be sha256 followed by 64 lowercase hex characters")
    return value
