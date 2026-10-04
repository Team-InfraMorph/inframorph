import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

from .contracts import REGION, BuildArtifact, FoundationOutputs, Plan, validate_contracts
from .deployment import DeploymentOrchestrator, DeploymentRequest
from .errors import AdapterError
from .events import EventEmitter
from .gcp_api import IMPERSONATION_ENV, GcpApi
from .image import ImagePublisher
from .locking import AppLock
from .naming import AppIdentity
from .process import Runner
from .records import DeploymentRecord
from .terraform import TerraformManager, expected_resource_categories, terraform_values


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODULE = ROOT / "terraform" / "gcp" / "app"

# Control Plane contract: one state folder per Plan.app (<INFRAMORPH_HOME>/state/<app>), same as the other adapters.
STATE_RECORD = "gcp-deployment.json"
STATE_WORK_DIR = "gcp-work"
STATE_PLAN = "plan.gcp.json"
STATE_ARTIFACT = "build.gcp.json"


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


def _common(parser: argparse.ArgumentParser, contracts_required: bool = True) -> None:
    _argument_from_env(parser, "--plan", "INFRAMORPH_PLAN", required=contracts_required, type=Path)
    _argument_from_env(parser, "--artifact", "INFRAMORPH_BUILD_ARTIFACT", required=contracts_required, type=Path)
    _argument_from_env(parser, "--foundation", "INFRAMORPH_GCP_FOUNDATION_OUTPUTS", required=True, type=Path)
    _argument_from_env(parser, "--project-id", "INFRAMORPH_GCP_PROJECT_ID", required=True)
    parser.add_argument(
        "--region",
        default=_env("GCP_REGION") or REGION,
        help="GCP region (environment: GCP_REGION)",
    )


def _state_dir_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="per-app state folder from the Control Plane; holds the record, work dir and last Plan/Artifact",
    )


def _timeout_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=_env("INFRAMORPH_DEPLOY_TIMEOUT_SECONDS") or 900,
        help="deployment timeout (environment: INFRAMORPH_DEPLOY_TIMEOUT_SECONDS)",
    )


def _resolve_state_paths(args: argparse.Namespace) -> None:
    """--state-dir decides record/work-dir (and rollback inputs) so the Control Plane passes one folder only."""
    state_dir = getattr(args, "state_dir", None)
    if state_dir is not None:
        if hasattr(args, "record"):
            args.record = state_dir / STATE_RECORD
        if hasattr(args, "work_dir"):
            args.work_dir = state_dir / STATE_WORK_DIR
        if args.command == "rollback":
            args.plan = args.plan or state_dir / STATE_PLAN
            args.artifact = args.artifact or state_dir / STATE_ARTIFACT
        if args.command in ("deploy", "rollback"):
            state_dir.mkdir(parents=True, exist_ok=True)
    if args.command not in ("deploy", "rollback"):
        return
    missing = [
        option for option, name in (("--record", "record"), ("--work-dir", "work_dir"))
        if getattr(args, name) is None
    ]
    if missing:
        raise AdapterError("{} required (or pass --state-dir)".format(" and ".join(missing)))
    if args.plan is None or args.artifact is None:
        raise AdapterError("--plan and --artifact are required (rollback can read them from --state-dir)")
    if args.command == "rollback" and not (args.plan.exists() and args.artifact.exists()):
        raise AdapterError(
            "rollback needs the Plan and BuildArtifact of the last successful deployment; "
            "none were saved in {}".format(state_dir)
        )


def _save_inputs(args: argparse.Namespace) -> None:
    """Keep the inputs of a successful deploy next to its record so a later rollback needs only --state-dir."""
    for source, name in ((args.plan, STATE_PLAN), (args.artifact, STATE_ARTIFACT)):
        target = args.state_dir / name
        if source.resolve() == target.resolve():
            continue
        temporary = target.with_suffix(target.suffix + ".tmp")
        shutil.copyfile(str(source), str(temporary))
        os.chmod(str(temporary), 0o600)
        temporary.replace(target)


def _contracts(args: argparse.Namespace) -> Tuple[Plan, BuildArtifact, FoundationOutputs]:
    return validate_contracts(args.plan, args.artifact, args.foundation, args.project_id, args.region)


def deployer_environment(foundation: FoundationOutputs) -> Dict[str, str]:
    """Every gcloud, Terraform and Docker call runs as the Foundation deployer.

    The operator's own login only mints short-lived tokens for it; no key file
    is read and the operator's broader roles are never used for a deployment.
    """
    return {
        IMPERSONATION_ENV: foundation.deployer_service_account_email,
        "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT": foundation.deployer_service_account_email,
        "CLOUDSDK_CORE_PROJECT": foundation.project_id,
        "CLOUDSDK_CORE_DISABLE_PROMPTS": "1",
        "GOOGLE_PROJECT": foundation.project_id,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="InfraMorph GCP Adapter")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="validate all deployment contracts")
    _common(validate)

    preview = commands.add_parser("plan", help="show expected changes; optionally run terraform plan")
    _common(preview)
    _argument_from_env(preview, "--migration-command", "INFRAMORPH_MIGRATION_COMMAND")
    _argument_from_env(preview, "--state-bucket", "INFRAMORPH_GCP_TF_STATE_BUCKET")
    _argument_from_env(preview, "--work-dir", "INFRAMORPH_GCP_WORK_DIR", type=Path)
    preview.add_argument("--terraform-plan", action="store_true")
    _state_dir_argument(preview)
    preview.add_argument("--module-dir", type=Path, default=DEFAULT_MODULE)

    deploy = commands.add_parser("deploy", help="publish the image and deploy the app to Cloud Run")
    _common(deploy)
    _argument_from_env(deploy, "--deployment-id", "INFRAMORPH_DEPLOYMENT_ID", required=True)
    _argument_from_env(deploy, "--migration-command", "INFRAMORPH_MIGRATION_COMMAND")
    _argument_from_env(deploy, "--state-bucket", "INFRAMORPH_GCP_TF_STATE_BUCKET", required=True)
    _argument_from_env(deploy, "--work-dir", "INFRAMORPH_GCP_WORK_DIR", type=Path)
    _argument_from_env(deploy, "--record", "INFRAMORPH_GCP_DEPLOYMENT_RECORD", type=Path)
    _state_dir_argument(deploy)
    _timeout_argument(deploy)
    deploy.add_argument("--migration-backward-compatible", action="store_true")
    deploy.add_argument("--module-dir", type=Path, default=DEFAULT_MODULE)
    deploy.add_argument("--execute", action="store_true")

    rollback = commands.add_parser("rollback", help="restore a recorded successful digest via Terraform")
    _common(rollback, contracts_required=False)
    _argument_from_env(rollback, "--deployment-id", "INFRAMORPH_DEPLOYMENT_ID", required=True)
    _argument_from_env(rollback, "--record", "INFRAMORPH_GCP_DEPLOYMENT_RECORD", type=Path)
    _argument_from_env(rollback, "--state-bucket", "INFRAMORPH_GCP_TF_STATE_BUCKET", required=True)
    _argument_from_env(rollback, "--work-dir", "INFRAMORPH_GCP_WORK_DIR", type=Path)
    _state_dir_argument(rollback)
    _timeout_argument(rollback)
    rollback.add_argument("--module-dir", type=Path, default=DEFAULT_MODULE)
    rollback.add_argument("--execute", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    contracts_loaded = False
    try:
        _resolve_state_paths(args)
        plan, artifact, foundation = _contracts(args)
        contracts_loaded = True
        identity = AppIdentity.from_app(plan.app)
        hostname = identity.hostname(foundation.apps_domain) if foundation.apps_domain else None
        if args.command == "validate":
            print(json.dumps({
                "valid": True,
                "app_id": identity.app_id,
                "source_revision": plan.source_revision,
                "hostname": hostname,
                "state_prefix": identity.state_prefix,
                "deployer": foundation.deployer_service_account_email,
            }, indent=2))
            return 0

        runner = Runner(deployer_environment(foundation))
        if args.command == "plan":
            output = expected_resource_categories(plan)
            output.update({"hostname": hostname, "state_prefix": identity.state_prefix})
            if args.terraform_plan:
                if not args.state_bucket or not args.work_dir:
                    raise AdapterError("--terraform-plan requires --state-bucket and --work-dir")
                local = ImagePublisher(runner).inspect(artifact)
                preview_digest = hashlib.sha256(local.image_id.encode("utf-8")).hexdigest()
                preview_uri = "{}@sha256:{}".format(ImagePublisher.repository(foundation), preview_digest)
                values = terraform_values(
                    plan, foundation, identity,
                    deployment_image_uri=preview_uri,
                    service_image_uri=preview_uri,
                    migration_command=args.migration_command,
                    activate_services=False,
                )
                manager = TerraformManager(args.module_dir, runner)
                manager.prepare(args.work_dir)
                manager.init(args.work_dir, args.state_bucket, identity.state_prefix)
                planned = manager.plan(args.work_dir, values)
                output["terraform_summary"] = planned.summary
                output["terraform_plan_file"] = str(planned.plan_file)
                output["preview_image_only"] = True
            print(json.dumps(output, indent=2, ensure_ascii=False))
            return 0

        if not args.execute:
            raise AdapterError("GCP mutation is disabled; pass --execute only after reviewing the Terraform plan")
        emitter = EventEmitter(args.deployment_id)
        orchestrator = DeploymentOrchestrator(
            plan,
            artifact,
            foundation,
            TerraformManager(args.module_dir, runner),
            publisher=ImagePublisher(runner),
            gcp=GcpApi(foundation, runner),
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
                record = orchestrator.deploy(request)
            if args.state_dir is not None:
                _save_inputs(args)
            # stdout stays DeployEvent JSONL only; the human-readable result goes to stderr.
            print("GCP Adapter: {} deploy complete: {}".format(record.deploy_mode, record.url), file=sys.stderr)
            return 0
        record = DeploymentRecord.load(args.record)
        if record is None:
            raise AdapterError("rollback record does not exist")
        emitter.emit("rollback", "started", detail="explicit rollback requested")
        with AppLock(args.record.with_suffix(args.record.suffix + ".lock")):
            orchestrator.rollback_record(record, args.work_dir, args.state_bucket, args.timeout_seconds)
        emitter.emit(
            "rollback", "ok",
            detail=json.dumps({"restored_digest": record.image_digest_uri, "revisions": record.revisions}),
            url=record.url,
        )
        return 0
    except (AdapterError, OSError, ValueError) as exc:
        if not contracts_loaded and getattr(args, "deployment_id", None):
            EventEmitter(args.deployment_id).emit(
                "plan", "fail", detail="contract validation failed: {}".format(str(exc))[:3000]
            )
        print("GCP Adapter: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
