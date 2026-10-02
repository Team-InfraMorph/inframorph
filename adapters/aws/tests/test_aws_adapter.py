import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adapters.aws.cli import build_parser
from adapters.aws.contracts import BuildArtifact, FoundationOutputs, Plan, validate_contracts
from adapters.aws.errors import ContractError, DeploymentError
from adapters.aws.events import EventEmitter
from adapters.aws.image import ImagePublisher
from adapters.aws.locking import AppLock
from adapters.aws.naming import AppIdentity
from adapters.aws.process import CommandResult
from adapters.aws.terraform import expected_resource_categories, terraform_values


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
# Test-only placeholder with the same 40-character shape as a full Git SHA.
SHA = "ebad709867cf1f3523075d8038ae41bd69ecae9b"


def adapter_environment():
    return {
        "AWS_PROFILE": "inframorph-test",
        "AWS_REGION": "ap-northeast-2",
        "INFRAMORPH_AWS_ACCOUNT_ID": "111122223333",
        "INFRAMORPH_PLAN": "/tmp/plan.aws.json",
        "INFRAMORPH_BUILD_ARTIFACT": "/tmp/build-artifact.json",
        "INFRAMORPH_FOUNDATION_OUTPUTS": "/tmp/foundation-outputs.json",
        "INFRAMORPH_TF_STATE_BUCKET": "inframorph-tfstate-example",
        "INFRAMORPH_AWS_WORK_DIR": "/tmp/inframorph-app",
        "INFRAMORPH_DEPLOYMENT_RECORD": "/tmp/aws-deployment.json",
        "INFRAMORPH_DEPLOYMENT_ID": "deploy-test-1",
        "INFRAMORPH_MIGRATION_COMMAND": "npx prisma migrate deploy",
        "INFRAMORPH_DEPLOY_TIMEOUT_SECONDS": "600",
    }


def plan_data():
    return {
        "schema_version": "1.0.0",
        "source_revision": SHA,
        "target": "aws",
        "app": "demo-app",
        "image_tag": "app:" + SHA,
        "services": [
            {
                "name": "web",
                "kind": "http",
                "cpu": 256,
                "mem": 512,
                "port": 3000,
                "health": "/health",
                "public": True,
                "command": None,
            },
            {
                "name": "worker",
                "kind": "worker",
                "cpu": 256,
                "mem": 512,
                "port": None,
                "health": None,
                "public": False,
                "command": "node src/worker.js",
            },
        ],
        "db": {"type": "rds_postgres", "patch": "sqlite_to_postgres"},
        "storage": {"type": "s3", "patch": "fs_to_storage", "path": "uploads/"},
        "secrets": ["DATABASE_URL"],
        "config": {"STORAGE_DRIVER": "s3"},
        "logs": "cloudwatch",
        "est_monthly_krw": None,
        "mermaid": "flowchart LR\nweb --> db\nworker --> db",
    }


def artifact_data():
    return {
        "schema_version": "1.0.0",
        "source_revision": SHA,
        "target": "aws",
        "image": "app:" + SHA,
        "platform": "linux/amd64",
    }


def foundation_data():
    values = {
        "region": "ap-northeast-2",
        "account_id": "111122223333",
        "vpc_id": "vpc-0123abcd",
        "private_subnet_ids": ["subnet-0123abcd", "subnet-4567abcd"],
        "alb_dns_name": "inframorph-alb.example.elb.amazonaws.com",
        "alb_security_group_id": "sg-0123abcd",
        "https_listener_arn": "arn:aws:elasticloadbalancing:ap-northeast-2:111122223333:listener/app/im/1/2",
        "apps_wildcard_domain": "*.apps.example.com",
        "acm_certificate_status": "ISSUED",
        "ecs_cluster_name": "inframorph",
        "ecs_cluster_arn": "arn:aws:ecs:ap-northeast-2:111122223333:cluster/inframorph",
        "ecr_repository_name": "inframorph/apps",
        "ecr_repository_url": "111122223333.dkr.ecr.ap-northeast-2.amazonaws.com/inframorph/apps",
        "rds_address": "db.example.ap-northeast-2.rds.amazonaws.com",
        "rds_port": 5432,
        "rds_db_name": "app",
        "rds_security_group_id": "sg-4567abcd",
        "rds_master_secret_arn": "arn:aws:secretsmanager:ap-northeast-2:111122223333:secret:rds-master-AbCd",
    }
    return {key: {"sensitive": False, "type": "string", "value": value} for key, value in values.items()}


class CliEnvironmentTests(unittest.TestCase):
    def test_environment_supplies_runtime_inputs(self):
        with patch.dict("os.environ", adapter_environment(), clear=True):
            args = build_parser().parse_args(["deploy"])
        self.assertEqual(args.account_id, "111122223333")
        self.assertEqual(args.plan, Path("/tmp/plan.aws.json"))
        self.assertEqual(args.foundation, Path("/tmp/foundation-outputs.json"))
        self.assertEqual(args.state_bucket, "inframorph-tfstate-example")
        self.assertEqual(args.timeout_seconds, 600)
        self.assertFalse(args.execute)

    def test_cli_value_overrides_environment(self):
        environment = adapter_environment()
        with patch.dict("os.environ", environment, clear=True):
            args = build_parser().parse_args(
                ["validate", "--account-id", "999900001111"]
            )
        self.assertEqual(args.account_id, "999900001111")

    def test_environment_cannot_enable_mutation_or_rollback_approval(self):
        environment = adapter_environment()
        environment["INFRAMORPH_EXECUTE"] = "true"
        environment["INFRAMORPH_MIGRATION_BACKWARD_COMPATIBLE"] = "true"
        with patch.dict("os.environ", environment, clear=True):
            args = build_parser().parse_args(["deploy"])
        self.assertFalse(args.execute)
        self.assertFalse(args.migration_backward_compatible)


class ContractTests(unittest.TestCase):
    def test_pr20_plan_and_artifact_contract(self):
        plan = Plan.parse(plan_data())
        artifact = BuildArtifact.parse(artifact_data())
        self.assertEqual(plan.app, "demo-app")
        self.assertEqual(artifact.platform, "linux/amd64")
        self.assertEqual([item.name for item in plan.services], ["web", "worker"])

    def test_unknown_plan_field_is_rejected(self):
        value = plan_data()
        value["adapter_guess"] = "forbidden"
        with self.assertRaisesRegex(ContractError, "unknown fields"):
            Plan.parse(value)

    def test_mutable_or_wrong_artifact_is_rejected(self):
        value = artifact_data()
        value["image"] = "app:latest"
        with self.assertRaisesRegex(ContractError, "app:<source_revision>"):
            BuildArtifact.parse(value)

    def test_public_worker_is_rejected(self):
        value = plan_data()
        value["services"][1]["public"] = True
        with self.assertRaisesRegex(ContractError, "worker must be private"):
            Plan.parse(value)

    def test_foundation_requires_https_and_correct_target(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "foundation.json"
            value = foundation_data()
            value["acm_certificate_status"]["value"] = "PENDING_VALIDATION"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "ISSUED"):
                FoundationOutputs.from_file(path, "111122223333")

    def test_cross_contract_revision_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan = plan_data()
            artifact = artifact_data()
            artifact["source_revision"] = "a" * 40
            artifact["image"] = "app:" + "a" * 40
            for name, value in (("plan.json", plan), ("artifact.json", artifact), ("foundation.json", foundation_data())):
                (root / name).write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "do not match"):
                validate_contracts(
                    root / "plan.json",
                    root / "artifact.json",
                    root / "foundation.json",
                    "111122223333",
                )


class IdentityAndTerraformTests(unittest.TestCase):
    def setUp(self):
        self.plan = Plan.parse(plan_data())
        self.identity = AppIdentity.from_app(self.plan.app)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "foundation.json"
            path.write_text(json.dumps(foundation_data()), encoding="utf-8")
            self.foundation = FoundationOutputs.from_file(path, "111122223333")

    def test_identity_is_stable_across_revisions(self):
        again = AppIdentity.from_app("demo-app")
        self.assertEqual(self.identity, again)
        self.assertEqual(self.identity.state_key, "apps/demo-app/terraform.tfstate")
        self.assertEqual(self.identity.hostname(self.foundation.apps_domain), "demo-app.apps.example.com")

    def test_terraform_values_pin_digest_and_keep_workers_private(self):
        uri = self.foundation.ecr_repository_url + "@sha256:" + "a" * 64
        values = terraform_values(
            self.plan,
            self.foundation,
            self.identity,
            uri,
            uri,
            "npx prisma migrate deploy",
            False,
        )
        self.assertIn("@sha256:", values["service_image_uri"])
        worker = next(item for item in values["services"] if item["kind"] == "worker")
        self.assertFalse(worker["public"])
        self.assertIsNone(worker["port"])
        self.assertFalse(values["activate_services"])

    def test_db_requires_explicit_builder_migration_handoff(self):
        uri = self.foundation.ecr_repository_url + "@sha256:" + "a" * 64
        with self.assertRaisesRegex(ContractError, "migration command"):
            terraform_values(self.plan, self.foundation, self.identity, uri, uri, None, False)

    def test_expected_plan_never_changes_foundation(self):
        expected = expected_resource_categories(self.plan)
        self.assertEqual(expected["foundation_changes_expected"], 0)
        self.assertEqual(expected["mutable_image_tags_in_ecs"], 0)
        self.assertEqual(expected["public_worker_resources"], 0)

    def test_app_module_does_not_declare_foundation_resources(self):
        module = REPOSITORY_ROOT / "terraform" / "app"
        contents = "\n".join(path.read_text(encoding="utf-8") for path in module.glob("*.tf"))
        forbidden = [
            'resource "aws_vpc"',
            'resource "aws_subnet"',
            'resource "aws_lb" "',
            'resource "aws_lb_listener" "',
            'resource "aws_db_instance"',
            'resource "aws_ecr_repository"',
            'resource "aws_ecs_cluster"',
        ]
        for marker in forbidden:
            self.assertNotIn(marker, contents)
        self.assertNotIn("postgres:16-alpine", contents)
        service_block = (module / "ecs.tf").read_text(encoding="utf-8").split(
            'resource "aws_ecs_task_definition" "bootstrap"', 1
        )[0]
        self.assertNotIn("rds_master_secret_arn", service_block)


class EventTests(unittest.TestCase):
    def test_event_is_shared_schema_jsonl(self):
        stream = io.StringIO()
        emitter = EventEmitter("deploy-1", stream)
        emitter.emit("push", "ok", detail="sha256 digest recorded", duration_ms=10)
        value = json.loads(stream.getvalue())
        self.assertEqual(
            set(value),
            {"deployment_id", "ts", "target", "step", "status", "detail", "duration_ms"},
        )
        self.assertEqual(value["target"], "aws")

    def test_app_lock_serializes_full_pipeline(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "demo-app.lock"
            with AppLock(path):
                with self.assertRaisesRegex(DeploymentError, "already running"):
                    with AppLock(path):
                        pass


class FakeRunner:
    def __init__(self):
        self.calls = []

    def json(self, args, **kwargs):
        self.calls.append(list(args))
        if args[:3] == ["docker", "image", "inspect"]:
            return [{"Id": "sha256:" + "b" * 64, "Os": "linux", "Architecture": "amd64"}]
        raise AssertionError(args)

    def run(self, args, **kwargs):
        self.calls.append(list(args))
        if args[:3] == ["aws", "ecr", "get-login-password"]:
            return CommandResult("token", "", 0)
        if args[:3] == ["aws", "ecr", "describe-images"]:
            return CommandResult("sha256:" + "c" * 64 + "\n", "", 0)
        return CommandResult("", "", 0)


class ImagePublisherTests(unittest.TestCase):
    def test_publish_tags_exact_image_id_and_returns_registry_digest(self):
        artifact = BuildArtifact.parse(artifact_data())
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "foundation.json"
            path.write_text(json.dumps(foundation_data()), encoding="utf-8")
            foundation = FoundationOutputs.from_file(path, "111122223333")
        runner = FakeRunner()
        publisher = ImagePublisher(runner)
        image = publisher.inspect(artifact)
        published = publisher.publish(
            image,
            artifact,
            foundation,
            build_id="12345678-abcd-1234-abcd-123456789abc",
        )
        tag_call = next(call for call in runner.calls if call[:2] == ["docker", "tag"])
        self.assertEqual(tag_call[2], "sha256:" + "b" * 64)
        self.assertIn("@sha256:", published.uri)
        self.assertNotIn(artifact.image + "@", published.uri)


if __name__ == "__main__":
    unittest.main()
