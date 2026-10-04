"""Strict validation for the PR #20 deployment contracts and Foundation outputs.

The shared schema PR is intentionally not copied into this branch.  This module
mirrors its frozen v1.0.0 wire contract and adds AWS-only cross checks.  When the
shared schemas land, callers can validate with ``schemas.validate`` first; this
validator remains the Adapter's security boundary.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .errors import ContractError
from schemas.plan import DbPlan as SharedDbPlan


SCHEMA_VERSION = "1.0.0"
REVISION_RE = re.compile(r"[0-9a-f]{40}")
NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,40}")
ENV_RE = re.compile(r"[A-Z][A-Z0-9_]*")
ACCOUNT_RE = re.compile(r"[0-9]{12}")
SG_RE = re.compile(r"sg-[0-9a-f]+")
SUBNET_RE = re.compile(r"subnet-[0-9a-f]+")
VPC_RE = re.compile(r"vpc-[0-9a-f]+")
SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")


def _load_object(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("{}: valid JSON object is required: {}".format(path, exc)) from exc
    if not isinstance(value, dict):
        raise ContractError("{}: top-level JSON value must be an object".format(path))
    return value


def _keys(
    value: Mapping[str, Any],
    label: str,
    required: Set[str],
    optional: Set[str],
) -> None:
    actual = set(value)
    missing = required - actual
    extra = actual - required - optional
    if missing:
        raise ContractError("{}: missing fields: {}".format(label, ", ".join(sorted(missing))))
    if extra:
        raise ContractError("{}: unknown fields: {}".format(label, ", ".join(sorted(extra))))


def _string(value: Any, label: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
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
    if NAME_RE.fullmatch(result) is None:
        raise ContractError(
            "{} must start with a lowercase letter and contain at most 41 lowercase letters, digits, or hyphens".format(label)
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


def _valid_fargate_size(cpu: int, memory: int) -> bool:
    allowed = {
        256: {512, 1024, 2048},
        512: set(range(1024, 4097, 1024)),
        1024: set(range(2048, 8193, 1024)),
        2048: set(range(4096, 16385, 1024)),
        4096: set(range(8192, 30721, 1024)),
        8192: set(range(16384, 61441, 4096)),
        16384: set(range(32768, 122881, 8192)),
    }
    return memory in allowed.get(cpu, set())


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
        _keys(
            value,
            label,
            {"name", "kind", "cpu", "mem"},
            {"port", "health", "public", "command"},
        )
        name = _name(value["name"], label + ".name")
        kind = _string(value["kind"], label + ".kind")
        if kind not in {"http", "worker"}:
            raise ContractError("{}.kind must be http or worker".format(label))
        cpu = _int(value["cpu"], label + ".cpu", 1)
        memory = _int(value["mem"], label + ".mem", 1)
        if not _valid_fargate_size(cpu, memory):
            raise ContractError("{} has an unsupported Fargate cpu/memory combination".format(label))
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
        if value["target"] != "aws":
            raise ContractError("plan.target must be aws")
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
        public_http = [item for item in services if item.kind == "http" and item.public]
        if len(public_http) != 1:
            raise ContractError("the MVP requires exactly one public http service")

        db = None
        raw_db = value.get("db")

        if raw_db is not None:
            if not isinstance(raw_db, dict):
                raise ContractError(
                    "plan.db must be an object or null"
                )

            try:
                checked = SharedDbPlan.model_validate(raw_db)
            except ValueError:
                raise ContractError(
                    "invalid database plan"
                ) from None

            if checked.type != "rds_postgres":
                raise ContractError(
                    "AWS database must be rds_postgres"
                )

            db = DbPlan(
                type=checked.type,
                patch=checked.patch,
                source_engine=checked.source_engine,
                target_engine=checked.target_engine,
                orm=checked.orm,
            )
        storage = None
        raw_storage = value.get("storage")
        if raw_storage is not None:
            if not isinstance(raw_storage, dict):
                raise ContractError("plan.storage must be an object or null")
            _keys(raw_storage, "plan.storage", {"type", "patch", "path"}, set())
            path = _string(raw_storage["path"], "plan.storage.path")
            if path.startswith("/") or "\\" in path or any(part in {"", ".", ".."} for part in path.rstrip("/").split("/")):
                raise ContractError("plan.storage.path must be a repository-relative path")
            if raw_storage["type"] != "s3" or raw_storage["patch"] != "fs_to_storage":
                raise ContractError("AWS storage must be s3 with fs_to_storage patch")
            storage = StoragePlan(raw_storage["type"], raw_storage["patch"], path)

        secrets = _env_names(value.get("secrets", []), "plan.secrets")
        config = _config(value.get("config", {}), "plan.config")
        if db is not None and "DATABASE_URL" not in secrets:
            raise ContractError("a database plan requires DATABASE_URL in plan.secrets")
        if storage is not None and config.get("STORAGE_DRIVER") != "s3":
            raise ContractError("an S3 storage plan requires STORAGE_DRIVER=s3")
        logs = _string(value["logs"], "plan.logs")
        if logs != "cloudwatch":
            raise ContractError("AWS plan.logs must be cloudwatch")
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
        _keys(
            value,
            "build_artifact",
            {"source_revision", "target", "image", "platform"},
            {"schema_version"},
        )
        version = value.get("schema_version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            raise ContractError("build_artifact.schema_version must be {}".format(SCHEMA_VERSION))
        if value["target"] != "aws":
            raise ContractError("build_artifact.target must be aws")
        revision = _revision(value["source_revision"], "build_artifact.source_revision")
        image = _string(value["image"], "build_artifact.image")
        if image != "app:{}".format(revision):
            raise ContractError("build_artifact.image must be app:<source_revision>")
        platform = _string(value["platform"], "build_artifact.platform")
        if platform != "linux/amd64":
            raise ContractError("build_artifact.platform must be linux/amd64")
        return cls(version, revision, image, platform)


FOUNDATION_REQUIRED = {
    "region",
    "account_id",
    "vpc_id",
    "private_subnet_ids",
    "alb_dns_name",
    "alb_security_group_id",
    "https_listener_arn",
    "apps_wildcard_domain",
    "acm_certificate_status",
    "ecs_cluster_name",
    "ecs_cluster_arn",
    "ecr_repository_name",
    "ecr_repository_url",
    "rds_address",
    "rds_port",
    "rds_db_name",
    "rds_security_group_id",
    "rds_master_secret_arn",
}


def _terraform_output_values(value: Mapping[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, dict) and "value" in item and set(item).issubset({"value", "type", "sensitive"}):
            result[key] = item["value"]
        else:
            result[key] = item
    return result


@dataclass(frozen=True)
class FoundationOutputs:
    region: str
    account_id: str
    vpc_id: str
    private_subnet_ids: Tuple[str, ...]
    alb_dns_name: str
    alb_security_group_id: str
    https_listener_arn: str
    apps_domain: str
    ecs_cluster_name: str
    ecs_cluster_arn: str
    ecr_repository_name: str
    ecr_repository_url: str
    rds_address: str
    rds_port: int
    rds_db_name: str
    rds_security_group_id: str
    rds_master_secret_arn: str

    @classmethod
    def from_file(
        cls,
        path: Path,
        expected_account_id: str,
        expected_region: str = "ap-northeast-2",
    ) -> "FoundationOutputs":
        raw = _terraform_output_values(_load_object(path))
        missing = FOUNDATION_REQUIRED - set(raw)
        if missing:
            raise ContractError("foundation outputs missing: {}".format(", ".join(sorted(missing))))
        region = _string(raw["region"], "foundation.region")
        account_id = _string(raw["account_id"], "foundation.account_id")
        if ACCOUNT_RE.fullmatch(account_id) is None:
            raise ContractError("foundation.account_id must be 12 digits")
        if region != expected_region:
            raise ContractError("foundation region {} does not match {}".format(region, expected_region))
        if account_id != expected_account_id:
            raise ContractError("foundation account {} does not match {}".format(account_id, expected_account_id))
        vpc_id = _string(raw["vpc_id"], "foundation.vpc_id")
        if VPC_RE.fullmatch(vpc_id) is None:
            raise ContractError("foundation.vpc_id is invalid")
        subnet_values = raw["private_subnet_ids"]
        if not isinstance(subnet_values, list) or len(subnet_values) < 2:
            raise ContractError("foundation.private_subnet_ids must contain at least two private subnets")
        private_subnets = tuple(_string(item, "foundation.private_subnet_ids") for item in subnet_values)
        if any(SUBNET_RE.fullmatch(item) is None for item in private_subnets):
            raise ContractError("foundation.private_subnet_ids contains an invalid subnet ID")
        if len(set(private_subnets)) != len(private_subnets):
            raise ContractError("foundation.private_subnet_ids must be distinct")
        listener = raw["https_listener_arn"]
        if not isinstance(listener, str) or ":listener/app/" not in listener:
            raise ContractError("foundation HTTPS listener is not ready")
        if raw["acm_certificate_status"] != "ISSUED":
            raise ContractError("foundation ACM certificate must be ISSUED")
        wildcard = _string(raw["apps_wildcard_domain"], "foundation.apps_wildcard_domain")
        if not wildcard.startswith("*.") or wildcard.count("*") != 1:
            raise ContractError("foundation.apps_wildcard_domain must be a single-label wildcard")
        alb_sg = _string(raw["alb_security_group_id"], "foundation.alb_security_group_id")
        rds_sg = _string(raw["rds_security_group_id"], "foundation.rds_security_group_id")
        if SG_RE.fullmatch(alb_sg) is None or SG_RE.fullmatch(rds_sg) is None:
            raise ContractError("foundation security group output is invalid")
        rds_port = _int(raw["rds_port"], "foundation.rds_port", 1, 65535)
        if rds_port != 5432:
            raise ContractError("foundation RDS port must be 5432")
        ecr_url = _string(raw["ecr_repository_url"], "foundation.ecr_repository_url")
        if not ecr_url.startswith(account_id + ".dkr.ecr." + region + ".amazonaws.com/"):
            raise ContractError("foundation ECR repository does not match target account and region")
        master_secret = _string(raw["rds_master_secret_arn"], "foundation.rds_master_secret_arn")
        if ":secretsmanager:" + region + ":" + account_id + ":secret:" not in master_secret:
            raise ContractError("foundation master secret ARN does not match target account and region")
        return cls(
            region=region,
            account_id=account_id,
            vpc_id=vpc_id,
            private_subnet_ids=private_subnets,
            alb_dns_name=_string(raw["alb_dns_name"], "foundation.alb_dns_name"),
            alb_security_group_id=alb_sg,
            https_listener_arn=listener,
            apps_domain=wildcard[2:],
            ecs_cluster_name=_string(raw["ecs_cluster_name"], "foundation.ecs_cluster_name"),
            ecs_cluster_arn=_string(raw["ecs_cluster_arn"], "foundation.ecs_cluster_arn"),
            ecr_repository_name=_string(raw["ecr_repository_name"], "foundation.ecr_repository_name"),
            ecr_repository_url=ecr_url,
            rds_address=_string(raw["rds_address"], "foundation.rds_address"),
            rds_port=rds_port,
            rds_db_name=_string(raw["rds_db_name"], "foundation.rds_db_name"),
            rds_security_group_id=rds_sg,
            rds_master_secret_arn=master_secret,
        )


def validate_contracts(
    plan_path: Path,
    artifact_path: Path,
    foundation_path: Path,
    expected_account_id: str,
    expected_region: str = "ap-northeast-2",
) -> Tuple[Plan, BuildArtifact, FoundationOutputs]:
    plan = Plan.from_file(plan_path)
    artifact = BuildArtifact.from_file(artifact_path)
    foundation = FoundationOutputs.from_file(foundation_path, expected_account_id, expected_region)
    if plan.source_revision != artifact.source_revision:
        raise ContractError("Plan and BuildArtifact source_revision values do not match")
    if plan.image_tag != artifact.image:
        raise ContractError("Plan image_tag and BuildArtifact image do not match")
    return plan, artifact, foundation


def validate_digest(value: str) -> str:
    if SHA256_RE.fullmatch(value) is None:
        raise ContractError("registry digest must be sha256 followed by 64 lowercase hex characters")
    return value
