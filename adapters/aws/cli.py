import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Optional, Sequence, Tuple

from .contracts import BuildArtifact, FoundationOutputs, Plan, validate_contracts
from .deployment import DeploymentOrchestrator, DeploymentRequest
from .errors import AdapterError
from .events import EventEmitter
from .image import ImagePublisher
from .locking import AppLock
from .naming import AppIdentity
from .records import DeploymentRecord
from .terraform import TerraformManager, expected_resource_categories, terraform_values


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODULE = ROOT / "terraform" / "app"


def _env(name: str) -> Optional[str]:
    value = os.environ.get(name)
    return value if value else None


def _argument_from_env(
    parser: argparse.ArgumentParser,
    option: str,
    env_name: str,
    required: bool = False,
    **kwargs: object,
) -> None:
    default = _env(env_name)
    help_text = str(kwargs.pop("help", ""))
    suffix = "environment: {}".format(env_name)
    parser.add_argument(
        option,
        default=default,
        required=required and default is None,
        help="{} ({})".format(help_text, suffix) if help_text else suffix,
        **kwargs,
    )


def _common(parser: argparse.ArgumentParser) -> None:
    _argument_from_env(parser, "--plan", "INFRAMORPH_PLAN", required=True, type=Path)
    _argument_from_env(parser, "--artifact", "INFRAMORPH_BUILD_ARTIFACT", required=True, type=Path)
    _argument_from_env(
        parser,
        "--foundation",
        "INFRAMORPH_FOUNDATION_OUTPUTS",
        required=True,
        type=Path,
    )
    _argument_from_env(parser, "--account-id", "INFRAMORPH_AWS_ACCOUNT_ID", required=True)
    parser.add_argument(
        "--region",
        default=_env("AWS_REGION") or _env("AWS_DEFAULT_REGION") or "ap-northeast-2",
        help="AWS region (environment: AWS_REGION or AWS_DEFAULT_REGION)",
    )


def _contracts(args: argparse.Namespace) -> Tuple[Plan, BuildArtifact, FoundationOutputs]:
    return validate_contracts(args.plan, args.artifact, args.foundation, args.account_id, args.region)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="InfraMorph AWS Adapter")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="validate all deployment contracts")
    _common(validate)

    preview = commands.add_parser("plan", help="show expected changes; optionally run terraform plan")
    _common(preview)
    _argument_from_env(preview, "--migration-command", "INFRAMORPH_MIGRATION_COMMAND")
    _argument_from_env(preview, "--state-bucket", "INFRAMORPH_TF_STATE_BUCKET")
    _argument_from_env(preview, "--work-dir", "INFRAMORPH_AWS_WORK_DIR", type=Path)
    preview.add_argument("--terraform-plan", action="store_true")
    preview.add_argument("--module-dir", type=Path, default=DEFAULT_MODULE)

    deploy = commands.add_parser("deploy", help="execute ECR publish and App deployment")
    _common(deploy)
    _argument_from_env(deploy, "--deployment-id", "INFRAMORPH_DEPLOYMENT_ID", required=True)
    _argument_from_env(deploy, "--migration-command", "INFRAMORPH_MIGRATION_COMMAND")
    _argument_from_env(deploy, "--state-bucket", "INFRAMORPH_TF_STATE_BUCKET", required=True)
    _argument_from_env(
        deploy,
        "--work-dir",
        "INFRAMORPH_AWS_WORK_DIR",
        required=True,
        type=Path,
    )
    _argument_from_env(
        deploy,
        "--record",
        "INFRAMORPH_DEPLOYMENT_RECORD",
        required=True,
        type=Path,
    )
    deploy.add_argument(
        "--timeout-seconds",
        type=int,
        default=_env("INFRAMORPH_DEPLOY_TIMEOUT_SECONDS") or 900,
        help="deployment timeout (environment: INFRAMORPH_DEPLOY_TIMEOUT_SECONDS)",
    )
    deploy.add_argument("--migration-backward-compatible", action="store_true")
    deploy.add_argument("--module-dir", type=Path, default=DEFAULT_MODULE)
    deploy.add_argument("--execute", action="store_true")

    rollback = commands.add_parser("rollback", help="restore a recorded successful digest via Terraform")
    _common(rollback)
    _argument_from_env(rollback, "--deployment-id", "INFRAMORPH_DEPLOYMENT_ID", required=True)
    _argument_from_env(
        rollback,
        "--record",
        "INFRAMORPH_DEPLOYMENT_RECORD",
        required=True,
        type=Path,
    )
    _argument_from_env(rollback, "--state-bucket", "INFRAMORPH_TF_STATE_BUCKET", required=True)
    _argument_from_env(
        rollback,
        "--work-dir",
        "INFRAMORPH_AWS_WORK_DIR",
        required=True,
        type=Path,
    )
    rollback.add_argument(
        "--timeout-seconds",
        type=int,
        default=_env("INFRAMORPH_DEPLOY_TIMEOUT_SECONDS") or 900,
        help="deployment timeout (environment: INFRAMORPH_DEPLOY_TIMEOUT_SECONDS)",
    )
    rollback.add_argument("--module-dir", type=Path, default=DEFAULT_MODULE)
    rollback.add_argument("--execute", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    contracts_loaded = False
    try:
        plan, artifact, foundation = _contracts(args)
        contracts_loaded = True
        identity = AppIdentity.from_app(plan.app)
        if args.command == "validate":
            print(json.dumps({
                "valid": True,
                "app_id": identity.app_id,
                "source_revision": plan.source_revision,
                "hostname": identity.hostname(foundation.apps_domain),
                "state_key": identity.state_key,
            }, indent=2))
            return 0

        if args.command == "plan":
            output = expected_resource_categories(plan)
            output.update({
                "hostname": identity.hostname(foundation.apps_domain),
                "state_key": identity.state_key,
                "listener_priority": identity.listener_priority,
            })
            if args.terraform_plan:
                if not args.state_bucket or not args.work_dir:
                    raise AdapterError("--terraform-plan requires --state-bucket and --work-dir")
                local = ImagePublisher().inspect(artifact)
                preview_digest = hashlib.sha256(local.image_id.encode("utf-8")).hexdigest()
                preview_uri = "{}@sha256:{}".format(foundation.ecr_repository_url, preview_digest)
                values = terraform_values(
                    plan, foundation, identity,
                    deployment_image_uri=preview_uri,
                    service_image_uri=preview_uri,
                    migration_command=args.migration_command,
                    activate_services=False,
                )
                manager = TerraformManager(args.module_dir)
                manager.prepare(args.work_dir)
                manager.init(args.work_dir, args.state_bucket, identity.state_key, foundation.region)
                planned = manager.plan(args.work_dir, values)
                output["terraform_summary"] = planned.summary
                output["terraform_plan_file"] = str(planned.plan_file)
                output["preview_image_only"] = True
            print(json.dumps(output, indent=2, ensure_ascii=False))
            return 0

        if not args.execute:
            raise AdapterError("AWS mutation is disabled; pass --execute only after reviewing the Terraform plan")
        emitter = EventEmitter(args.deployment_id)
        orchestrator = DeploymentOrchestrator(
            plan,
            artifact,
            foundation,
            TerraformManager(args.module_dir),
            events=emitter,
        )
        if args.command == "deploy":
            request = DeploymentRequest(
                deployment_id=args.deployment_id,
                state_bucket=args.state_bucket,
                migration_command=args.migration_command,
                timeout_seconds=args.timeout_seconds,
                migration_backward_compatible=args.migration_backward_compatible,
                work_dir=args.work_dir,
                record_path=args.record,
            )
            with AppLock(args.record.with_suffix(args.record.suffix + ".lock")):
                orchestrator.deploy(request)
            return 0
        record = DeploymentRecord.load(args.record)
        if record is None:
            raise AdapterError("rollback record does not exist")
        emitter.emit("rollback", "started", detail="explicit rollback requested")
        with AppLock(args.record.with_suffix(args.record.suffix + ".lock")):
            orchestrator.rollback_record(record, args.work_dir, args.state_bucket, args.timeout_seconds)
        emitter.emit(
            "rollback", "ok",
            detail=json.dumps({"restored_digest": record.image_digest_uri, "task_definitions": record.task_definitions}),
            url=record.url,
        )
        return 0
    except (AdapterError, OSError, ValueError) as exc:
        if not contracts_loaded and getattr(args, "deployment_id", None):
            EventEmitter(args.deployment_id).emit(
                "plan", "fail", detail="contract validation failed: {}".format(str(exc))[:3000]
            )
        print("AWS Adapter: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
