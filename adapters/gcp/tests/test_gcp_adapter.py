import copy
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adapters.gcp.cli import _resolve_state_paths, _save_inputs, build_parser, deployer_environment
from adapters.gcp.contracts import BuildArtifact, FoundationOutputs, Plan, validate_contracts
from adapters.gcp.deployment import DeploymentOrchestrator, DeploymentRequest
from adapters.gcp.errors import AdapterError, CommandError, ContractError, DeploymentError
from adapters.gcp.events import EventEmitter
from adapters.gcp.gcp_api import IMPERSONATION_ENV, GcpApi
from adapters.gcp.image import ImagePublisher, LocalImage, PublishedImage
from adapters.gcp.mode import FIRST, REDEPLOY, RESUME, detect_mode
from adapters.gcp.naming import AppIdentity
from adapters.gcp.process import CommandResult
from adapters.gcp.records import DeploymentRecord
from adapters.gcp.terraform import TerraformManager, TerraformPlan, expected_resource_categories, terraform_values


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
# Test-only placeholder with the same 40-character shape as a full Git SHA.
SHA = "ebad709867cf1f3523075d8038ae41bd69ecae9b"
DEPLOYER = "inframorph-deployer@example-project.iam.gserviceaccount.com"
REGISTRY = "asia-northeast3-docker.pkg.dev/example-project/apps"
DIGEST_URI = REGISTRY + "/app@sha256:" + "a" * 64
PREVIOUS_URI = REGISTRY + "/app@sha256:" + "b" * 64


def adapter_environment():
    return {
        "GCP_REGION": "asia-northeast3",
        "INFRAMORPH_GCP_PROJECT_ID": "example-project",
        "INFRAMORPH_PLAN": "/tmp/plan.gcp.json",
        "INFRAMORPH_BUILD_ARTIFACT": "/tmp/build-artifact.json",
        "INFRAMORPH_GCP_FOUNDATION_OUTPUTS": "/tmp/gcp-foundation-outputs.json",
        "INFRAMORPH_GCP_TF_STATE_BUCKET": "inframorph-tfstate-example",
        "INFRAMORPH_GCP_WORK_DIR": "/tmp/inframorph-gcp-app",
        "INFRAMORPH_GCP_DEPLOYMENT_RECORD": "/tmp/gcp-deployment.json",
        "INFRAMORPH_DEPLOYMENT_ID": "deploy-test-1",
        "INFRAMORPH_MIGRATION_COMMAND": "./node_modules/.bin/prisma db push --skip-generate",
        "INFRAMORPH_DEPLOY_TIMEOUT_SECONDS": "600",
    }


def plan_data(db=True, worker=True):
    services = [{
        "name": "web", "kind": "http", "cpu": 256, "mem": 512, "port": 3000,
        "health": "/health", "public": True, "command": None,
    }]
    if worker:
        services.append({
            "name": "worker", "kind": "worker", "cpu": 256, "mem": 512, "port": None,
            "health": None, "public": False, "command": "node src/worker.js",
        })
    value = {
        "schema_version": "1.0.0",
        "source_revision": SHA,
        "target": "gcp",
        "app": "demo-app",
        "image_tag": "app:" + SHA,
        "services": services,
        "db": {"type": "cloudsql_postgres", "patch": "sqlite_to_postgres"} if db else None,
        "storage": {"type": "gcs", "patch": "fs_to_storage", "path": "uploads/"},
        "secrets": ["DATABASE_URL"] if db else [],
        "config": {"STORAGE_DRIVER": "gcs"},
        "logs": "cloud_logging",
        "est_monthly_krw": None,
        "mermaid": "flowchart LR\nweb --> db",
    }
    return value


def artifact_data():
    return {
        "schema_version": "1.0.0",
        "source_revision": SHA,
        "target": "gcp",
        "image": "app:" + SHA,
        "platform": "linux/amd64",
    }


def foundation_values(load_balancer=False):
    return {
        "project_id": "example-project",
        "project_number": "123456789012",
        "region": "asia-northeast3",
        "network_id": "projects/example-project/global/networks/inframorph-vpc",
        "run_subnet_id": "projects/example-project/regions/asia-northeast3/subnetworks/inframorph-run-asia-northeast3",
        "artifact_registry_repository": "projects/example-project/locations/asia-northeast3/repositories/apps",
        "artifact_registry_url": REGISTRY,
        "dockerhub_proxy_url": "asia-northeast3-docker.pkg.dev/example-project/dockerhub",
        "cloudsql_instance_name": "inframorph-postgres",
        "cloudsql_connection_name": "example-project:asia-northeast3:inframorph-postgres",
        "cloudsql_private_ip": "10.61.0.3",
        "cloudsql_port": 5432,
        "cloudsql_db_name": "app",
        "cloudsql_master_username": "inframorph",
        "cloudsql_master_secret_id": "projects/example-project/secrets/inframorph-sql-master-password",
        "db_bootstrap_service_account_email": "inframorph-db-bootstrap@example-project.iam.gserviceaccount.com",
        "deployer_service_account_email": DEPLOYER,
        "app_resource_prefix": "im-",
        "load_balancer_enabled": load_balancer,
        "apps_wildcard_domain": "*.apps.example.com" if load_balancer else None,
        "lb_ip_address": None,
        "certificate_dns_authorization_records": [],
    }


def foundation_data(load_balancer=False):
    return {key: {"sensitive": False, "type": "string", "value": value}
            for key, value in foundation_values(load_balancer).items()}


def write_contracts(root: Path, plan=None, artifact=None, foundation=None):
    for name, value in (
        ("plan.json", plan or plan_data()),
        ("artifact.json", artifact or artifact_data()),
        ("foundation.json", foundation or foundation_data()),
    ):
        (root / name).write_text(json.dumps(value), encoding="utf-8")
    return root / "plan.json", root / "artifact.json", root / "foundation.json"


def load_foundation(load_balancer=False):
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "foundation.json"
        path.write_text(json.dumps(foundation_data(load_balancer)), encoding="utf-8")
        return FoundationOutputs.from_file(path, "example-project")


class CliEnvironmentTests(unittest.TestCase):
    def test_environment_supplies_runtime_inputs(self):
        with patch.dict("os.environ", adapter_environment(), clear=True):
            args = build_parser().parse_args(["deploy"])
        self.assertEqual(args.project_id, "example-project")
        self.assertEqual(args.foundation, Path("/tmp/gcp-foundation-outputs.json"))
        self.assertEqual(args.state_bucket, "inframorph-tfstate-example")
        self.assertEqual(args.timeout_seconds, 600)
        self.assertFalse(args.execute)

    def test_environment_cannot_enable_mutation_or_rollback_approval(self):
        environment = adapter_environment()
        environment["INFRAMORPH_EXECUTE"] = "true"
        environment["INFRAMORPH_MIGRATION_BACKWARD_COMPATIBLE"] = "true"
        with patch.dict("os.environ", environment, clear=True):
            args = build_parser().parse_args(["deploy"])
        self.assertFalse(args.execute)
        self.assertFalse(args.migration_backward_compatible)

    def test_every_command_impersonates_the_foundation_deployer(self):
        environment = deployer_environment(load_foundation())
        self.assertEqual(environment[IMPERSONATION_ENV], DEPLOYER)
        self.assertEqual(environment["GOOGLE_IMPERSONATE_SERVICE_ACCOUNT"], DEPLOYER)
        self.assertEqual(environment["CLOUDSDK_CORE_PROJECT"], "example-project")


class ContractTests(unittest.TestCase):
    def test_gcp_plan_and_artifact_contract(self):
        plan = Plan.parse(plan_data())
        artifact = BuildArtifact.parse(artifact_data())
        self.assertEqual(plan.public_service.name, "web")
        self.assertEqual(artifact.platform, "linux/amd64")

    def test_aws_plan_is_rejected(self):
        value = plan_data()
        value["target"] = "aws"
        with self.assertRaisesRegex(ContractError, "plan.target must be gcp"):
            Plan.parse(value)

    def test_aws_data_services_are_rejected(self):
        value = plan_data()
        value["db"] = {"type": "rds_postgres", "patch": "sqlite_to_postgres"}
        with self.assertRaisesRegex(ContractError, "cloudsql_postgres"):
            Plan.parse(value)
        value = plan_data()
        value["config"] = {"STORAGE_DRIVER": "s3"}
        with self.assertRaisesRegex(ContractError, "STORAGE_DRIVER=gcs"):
            Plan.parse(value)

    def test_cloud_run_size_limits(self):
        value = plan_data()
        value["services"][0]["mem"] = 8192
        with self.assertRaisesRegex(ContractError, "Cloud Run cpu/memory"):
            Plan.parse(value)
        value["services"][0]["cpu"] = 2048
        Plan.parse(value)

    def test_public_worker_is_rejected(self):
        value = plan_data()
        value["services"][1]["public"] = True
        with self.assertRaisesRegex(ContractError, "worker must be private"):
            Plan.parse(value)

    def test_foundation_must_match_project_and_prefix(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "foundation.json"
            path.write_text(json.dumps(foundation_data()), encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "does not match"):
                FoundationOutputs.from_file(path, "other-project")
            value = foundation_data()
            value["app_resource_prefix"]["value"] = "cp-"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "app_resource_prefix"):
                FoundationOutputs.from_file(path, "example-project")

    def test_foundation_rejects_foreign_resources(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "foundation.json"
            value = foundation_data()
            value["artifact_registry_url"]["value"] = "us-docker.pkg.dev/example-project/apps"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "Artifact Registry"):
                FoundationOutputs.from_file(path, "example-project")
            value = foundation_data()
            value["deployer_service_account_email"]["value"] = "deployer@other-project.iam.gserviceaccount.com"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "service account of project"):
                FoundationOutputs.from_file(path, "example-project")

    def test_load_balancer_foundation_provides_apps_domain(self):
        self.assertEqual(load_foundation(load_balancer=True).apps_domain, "apps.example.com")
        self.assertIsNone(load_foundation().apps_domain)

    def test_cross_contract_revision_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            artifact = artifact_data()
            artifact["source_revision"] = "a" * 40
            artifact["image"] = "app:" + "a" * 40
            paths = write_contracts(Path(temp), artifact=artifact)
            with self.assertRaisesRegex(ContractError, "do not match"):
                validate_contracts(*paths, "example-project")


class IdentityTests(unittest.TestCase):
    def test_identity_is_stable_and_inside_the_deployer_prefix(self):
        identity = AppIdentity.from_app("demo-app")
        self.assertEqual(identity, AppIdentity.from_app("demo-app"))
        self.assertTrue(identity.resource_prefix.startswith("im-demo-app-"))
        self.assertTrue(identity.runtime_service_account_id.startswith("im-"))
        self.assertEqual(identity.state_object(), "apps/demo-app/default.tfstate")
        self.assertTrue(identity.bucket_name("example-project").startswith("im-"))

    def test_long_app_ids_fit_gcp_name_limits(self):
        identity = AppIdentity.from_app("a" + "b" * 39 + "c")
        self.assertLessEqual(len(identity.resource_prefix), 32)
        self.assertLessEqual(len(identity.runtime_service_account_id), 30)
        self.assertGreaterEqual(len(identity.runtime_service_account_id), 6)
        self.assertLessEqual(len(identity.bucket_name("example-project")), 63)


class TerraformValueTests(unittest.TestCase):
    def setUp(self):
        self.foundation = load_foundation()
        self.identity = AppIdentity.from_app("demo-app")

    def values(self, plan=None, **kwargs):
        return terraform_values(
            plan or Plan.parse(plan_data()), self.foundation, self.identity,
            deployment_image_uri=kwargs.get("deployment", DIGEST_URI),
            service_image_uri=kwargs.get("service", DIGEST_URI),
            migration_command="./node_modules/.bin/prisma db push --skip-generate",
            activate_services=kwargs.get("active", True),
        )

    def test_values_match_the_app_module_variables_exactly(self):
        source = (REPOSITORY_ROOT / "terraform/gcp/app/variables.tf").read_text(encoding="utf-8")
        declared = set(re.findall(r'^variable "([a-z0-9_]+)"', source, re.MULTILINE))
        defaults_only = {"app_resource_prefix", "http_min_instances", "max_instances"}
        self.assertEqual(set(self.values()), declared - defaults_only)

    def test_images_must_be_digest_pinned(self):
        with self.assertRaisesRegex(ContractError, "digest-pinned"):
            self.values(service=REGISTRY + "/app:latest")

    def test_database_inputs_only_for_database_apps(self):
        values = self.values(Plan.parse(plan_data(db=False)))
        self.assertFalse(values["db_enabled"])
        self.assertIsNone(values["cloudsql_master_secret_id"])
        self.assertEqual(values["migration_command"], [])
        values = self.values()
        self.assertTrue(values["db_bootstrap_image"].startswith(self.foundation.dockerhub_proxy_url + "/library/postgres@sha256:"))

    def test_worker_command_is_split_without_a_shell(self):
        services = {item["name"]: item for item in self.values()["services"]}
        self.assertEqual(services["worker"]["command"], ["node", "src/worker.js"])
        self.assertEqual(services["web"]["command"], [])

    def test_expected_plan_never_changes_foundation(self):
        categories = expected_resource_categories(Plan.parse(plan_data()))
        self.assertEqual(categories["foundation_changes_expected"], 0)
        self.assertEqual(categories["per_app_load_balancer_resources"], 0)
        self.assertEqual(categories["worker_pools"], ["worker"])


class FakeRunner:
    def __init__(self, responses=None, env=None):
        self.calls = []
        self.inputs = []
        self.env = dict(env or {})
        self.responses = list(responses or [])

    def _next(self, args):
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return CommandResult("{}", "", 0)

    def run(self, args, cwd=None, input_text=None, env=None, check=True, sensitive=False):
        self.calls.append(list(args))
        self.inputs.append(input_text)
        result = self._next(args)
        if check and result.returncode != 0:
            raise CommandError("command failed: " + (result.stderr or result.stdout))
        return result

    def json(self, args, **kwargs):
        return json.loads(self.run(args, **kwargs).stdout)


class TerraformManagerTests(unittest.TestCase):
    def plan_output(self, changes):
        return CommandResult(json.dumps({"resource_changes": changes}), "", 0)

    def test_plan_refuses_foundation_resources(self):
        runner = FakeRunner([
            CommandResult("", "", 0),
            self.plan_output([{"address": "google_sql_database_instance.x", "type": "google_sql_database_instance",
                               "change": {"actions": ["update"]}}]),
        ])
        manager = TerraformManager(REPOSITORY_ROOT / "terraform/gcp/app", runner)
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ContractError, "Foundation"):
                manager.plan(Path(temp), {})

    def test_activation_after_staging_skips_refresh(self):
        runner = FakeRunner([CommandResult("", "", 0), self.plan_output([])])
        manager = TerraformManager(REPOSITORY_ROOT / "terraform/gcp/app", runner)
        with tempfile.TemporaryDirectory() as temp:
            manager.plan(Path(temp), {}, refresh=False)
        self.assertIn("-refresh=false", runner.calls[0])

    def test_state_must_live_under_apps(self):
        manager = TerraformManager(REPOSITORY_ROOT / "terraform/gcp/app", FakeRunner())
        with self.assertRaisesRegex(ContractError, "apps/"):
            manager.init(Path("/tmp"), "bucket", "foundation")


class ImagePublisherTests(unittest.TestCase):
    def test_publish_pushes_exact_image_id_with_short_lived_token(self):
        foundation = load_foundation()
        artifact = BuildArtifact.parse(artifact_data())
        runner = FakeRunner([
            CommandResult("ya29.token\n", "", 0),
            CommandResult("Login Succeeded", "", 0),
            CommandResult("", "", 0),
            CommandResult("", "", 0),
            CommandResult(json.dumps({"image_summary": {"digest": "sha256:" + "c" * 64}}), "", 0),
        ])
        image = LocalImage("app:" + SHA, "sha256:" + "d" * 64, "linux/amd64")
        published = ImagePublisher(runner).publish(image, artifact, foundation, build_id="0123abcd")
        login = runner.calls[1]
        self.assertEqual(login[:4], ["docker", "login", "--username", "oauth2accesstoken"])
        self.assertEqual(login[-1], "https://asia-northeast3-docker.pkg.dev")
        self.assertEqual(runner.inputs[1], "ya29.token")
        self.assertEqual(runner.calls[2][2], image.image_id)
        self.assertEqual(published.uri, REGISTRY + "/app@sha256:" + "c" * 64)


class GcpApiTests(unittest.TestCase):
    def api(self, responses=None, impersonate=True):
        env = {IMPERSONATION_ENV: DEPLOYER} if impersonate else {}
        api = GcpApi(load_foundation(), FakeRunner(responses, env))
        api._sleep = lambda seconds: None
        return api

    def test_caller_must_impersonate_the_deployer_in_the_right_project(self):
        with self.assertRaisesRegex(ContractError, "impersonate"):
            self.api(impersonate=False).verify_caller()
        with self.assertRaisesRegex(ContractError, "does not match"):
            self.api([CommandResult(json.dumps({"projectNumber": "999"}), "", 0)]).verify_caller()
        self.api([CommandResult(json.dumps({"projectNumber": "123456789012"}), "", 0)]).verify_caller()

    def test_state_exists_true_false_and_error(self):
        self.assertTrue(self.api([CommandResult("{}", "", 0)]).state_exists("b", "apps/x/default.tfstate"))
        self.assertFalse(self.api([CommandResult("", "ERROR: No URLs matched", 1)]).state_exists("b", "o"))
        with self.assertRaises(CommandError):
            self.api([CommandResult("", "ERROR: permission denied", 1)]).state_exists("b", "o")

    def test_job_retries_only_while_new_iam_grants_propagate(self):
        api = self.api([
            CommandError("Permission denied on secret: projects/1/secrets/im-x-database-url"),
            CommandResult("{}", "", 0),
        ])
        api.run_job("im-x-migration", "database migration")
        self.assertEqual(len(api.runner.calls), 2)
        api = self.api([CommandError("Execution failed: container exited with 1")])
        with self.assertRaisesRegex(DeploymentError, "resource.labels.job_name"):
            api.run_job("im-x-migration", "database migration")
        self.assertEqual(len(api.runner.calls), 1)

    def service(self, created="r2", ready="r2", percent=100, status="True"):
        return CommandResult(json.dumps({"status": {
            "conditions": [{"type": "Ready", "status": status, "message": "boom"}],
            "latestCreatedRevisionName": created,
            "latestReadyRevisionName": ready,
            "traffic": [{"revisionName": ready, "percent": percent}],
        }}), "", 0)

    def test_wait_services_requires_latest_revision_serving_all_traffic(self):
        api = self.api([self.service(ready="r1"), self.service(percent=50), self.service()])
        api.wait_services(["demo-app"], 60)
        self.assertEqual(len(api.runner.calls), 3)
        with self.assertRaisesRegex(DeploymentError, "boom"):
            self.api([self.service(status="False")]).wait_services(["demo-app"], 60)


class DeployModeTests(unittest.TestCase):
    def record(self):
        return DeploymentRecord("d1", "demo-app", SHA, "sha256:" + "d" * 64, PREVIOUS_URI, {}, {"web": "demo-app"},
                                "https://demo-app.a.run.app", "apps/demo-app", {})

    def test_modes(self):
        self.assertEqual(detect_mode("demo-app", None, False, {}, "apps/demo-app").mode, FIRST)
        self.assertEqual(detect_mode("demo-app", None, True, {}, "apps/demo-app").mode, RESUME)
        self.assertEqual(detect_mode("demo-app", self.record(), True, {}, "apps/demo-app").mode, REDEPLOY)
        with self.assertRaisesRegex(ContractError, "services are running"):
            detect_mode("demo-app", None, True, {"demo-app": 1}, "apps/demo-app")
        with self.assertRaisesRegex(ContractError, "state"):
            detect_mode("demo-app", self.record(), False, {}, "apps/demo-app")


class FakeTerraform:
    def __init__(self, fail_on_apply=None):
        self.calls = []
        self.applied = []
        self.fail_on_apply = fail_on_apply

    def prepare(self, work_dir):
        self.calls.append(("prepare",))

    def init(self, work_dir, bucket, prefix):
        self.calls.append(("init", prefix))

    def plan(self, work_dir, values, refresh=True):
        self.calls.append(("plan", values["activate_services"], refresh))
        return TerraformPlan(work_dir, work_dir / "app.tfplan", copy.deepcopy(values), {"create": 1, "update": 0, "delete": 0, "replace": 0})

    def apply(self, planned):
        self.calls.append(("apply", planned.values["activate_services"]))
        self.applied.append(planned.values)
        if self.fail_on_apply is not None and len(self.applied) == self.fail_on_apply:
            raise DeploymentError("apply failed")
        values = planned.values
        names = {item["name"]: ("demo-app" if item["public"] else "im-x-" + item["name"]) for item in values["services"]}
        return {
            "service_names": names if values["activate_services"] else {},
            "service_urls": {"web": "https://demo-app-xyz.a.run.app"} if values["activate_services"] else {},
            "external_url": "https://demo-app-xyz.a.run.app",
            "latest_revisions": {"web": "demo-app-00002"},
            "database_url_secret_id": "projects/example-project/secrets/im-x-database-url" if values["db_enabled"] else None,
            "database_password_secret_id": "projects/example-project/secrets/im-x-database-password" if values["db_enabled"] else None,
            "bootstrap_job_name": "im-x-db-bootstrap" if values["db_enabled"] else None,
            "migration_job_name": "im-x-migration" if values["db_enabled"] else None,
        }


class FakeGcp:
    def __init__(self, fail_job=None, state=False):
        self.calls = []
        self.secrets = {}
        self.fail_job = fail_job
        self.state = state

    def verify_caller(self):
        self.calls.append("verify")

    def state_exists(self, bucket, state_object):
        return self.state

    def live_services(self, names):
        return {}

    def put_secret(self, secret_id, value):
        self.calls.append(("secret", secret_id.rsplit("/", 1)[-1]))
        self.secrets[secret_id] = value

    def run_job(self, name, label):
        self.calls.append(("job", name))
        if name == self.fail_job:
            raise DeploymentError("{} failed".format(label))

    def wait_services(self, names, timeout):
        self.calls.append(("wait", tuple(names)))


class FakePublisher:
    def inspect(self, artifact):
        return LocalImage(artifact.image, "sha256:" + "d" * 64, "linux/amd64")

    def publish(self, image, artifact, foundation, build_id=None):
        return PublishedImage(image.image_id, "src-tag", "sha256:" + "a" * 64, DIGEST_URI)


class OrchestratorTests(unittest.TestCase):
    def run_deploy(self, plan=None, previous=None, terraform=None, gcp=None, compatible=False):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        record_path = root / "gcp-deployment.json"
        if previous is not None:
            previous.save(record_path)
        terraform = terraform or FakeTerraform()
        gcp = gcp or FakeGcp()
        # A successful previous deployment always left its Terraform state behind.
        gcp.state = previous is not None
        stream = io.StringIO()
        orchestrator = DeploymentOrchestrator(
            Plan.parse(plan or plan_data()), BuildArtifact.parse(artifact_data()), load_foundation(),
            terraform, publisher=FakePublisher(), gcp=gcp, events=EventEmitter("deploy-1", stream),
        )
        request = DeploymentRequest("deploy-1", "bucket", "./node_modules/.bin/prisma db push --skip-generate",
                                    600, compatible, root / "work", record_path)
        events = lambda: [json.loads(line) for line in stream.getvalue().splitlines()]
        with patch("adapters.gcp.deployment.external_https_smoke") as smoke:
            smoke.return_value.status = 200
            smoke.return_value.url = "https://demo-app-xyz.a.run.app/health"
            try:
                record = orchestrator.deploy(request)
            except Exception as exc:
                return exc, terraform, gcp, events()
        return record, terraform, gcp, events()

    def previous_record(self, db=True):
        values = terraform_values(Plan.parse(plan_data(db=db, worker=False)), load_foundation(),
                                  AppIdentity.from_app("demo-app"), PREVIOUS_URI, PREVIOUS_URI,
                                  "./node_modules/.bin/prisma db push --skip-generate", True)
        return DeploymentRecord("d0", "demo-app", "c" * 40, "sha256:" + "e" * 64, PREVIOUS_URI, {"web": "demo-app-00001"},
                                {"web": "demo-app"}, "https://demo-app-xyz.a.run.app", "apps/demo-app", values, "first")

    def test_first_database_deploy_keeps_staging_order(self):
        record, terraform, gcp, events = self.run_deploy()
        self.assertEqual(record.deploy_mode, FIRST)
        self.assertEqual(
            [call for call in terraform.calls if call[0] in ("plan", "apply")],
            [("plan", False, True), ("apply", False), ("plan", True, False), ("apply", True)],
        )
        self.assertEqual(gcp.calls[1:5], [
            ("secret", "im-x-database-password"), ("secret", "im-x-database-url"),
            ("job", "im-x-db-bootstrap"), ("job", "im-x-migration"),
        ])
        url = next(value for key, value in gcp.secrets.items() if key.endswith("database-url"))
        self.assertTrue(url.startswith("postgresql://app_demo_app_"))
        self.assertIn("@10.61.0.3:5432/", url)
        self.assertTrue(url.endswith("?sslmode=require"))
        self.assertEqual([e["step"] for e in events if e["status"] == "ok"][-1], "smoke")
        self.assertTrue(all(e["target"] == "gcp" for e in events))

    def test_redeploy_runs_only_migration_with_previous_services_staged(self):
        record, terraform, gcp, _ = self.run_deploy(previous=self.previous_record())
        self.assertEqual(record.deploy_mode, REDEPLOY)
        self.assertNotIn(("job", "im-x-db-bootstrap"), gcp.calls)
        self.assertIn(("job", "im-x-migration"), gcp.calls)
        self.assertFalse([call for call in gcp.calls if call[0] == "secret"])
        stage = terraform.applied[0]
        self.assertTrue(stage["activate_services"])
        self.assertEqual(stage["service_image_uri"], PREVIOUS_URI)
        self.assertEqual(stage["deployment_image_uri"], DIGEST_URI)
        self.assertEqual([item["name"] for item in stage["services"]], ["web"])
        self.assertEqual(terraform.applied[1]["service_image_uri"], DIGEST_URI)

    def test_app_without_database_activates_in_one_apply(self):
        record, terraform, gcp, _ = self.run_deploy(plan=plan_data(db=False))
        self.assertIsInstance(record, DeploymentRecord)
        self.assertEqual([call for call in terraform.calls if call[0] in ("plan", "apply")],
                         [("plan", True, True), ("apply", True)])
        self.assertFalse([call for call in gcp.calls if call[0] == "job"])

    def test_failed_migration_does_not_roll_back_code_without_compatibility(self):
        result, terraform, _, events = self.run_deploy(previous=self.previous_record(),
                                                       gcp=FakeGcp(fail_job="im-x-migration"))
        self.assertIsInstance(result, DeploymentError)
        self.assertEqual(len(terraform.applied), 1)
        self.assertEqual(events[-1]["step"], "rollback")
        self.assertEqual(events[-1]["status"], "fail")

    def test_failed_activation_restores_previous_values(self):
        result, terraform, gcp, events = self.run_deploy(plan=plan_data(db=False), previous=self.previous_record(db=False),
                                                         terraform=FakeTerraform(fail_on_apply=1))
        self.assertIsInstance(result, DeploymentError)
        self.assertEqual(terraform.applied[-1]["service_image_uri"], PREVIOUS_URI)
        self.assertEqual((events[-1]["step"], events[-1]["status"]), ("rollback", "ok"))

    def test_removing_the_database_is_refused_before_any_change(self):
        result, terraform, _, _ = self.run_deploy(plan=plan_data(db=False), previous=self.previous_record())
        self.assertIsInstance(result, ContractError)
        self.assertEqual(terraform.calls, [])


class ControlPlaneStateDirTests(unittest.TestCase):
    def test_deploy_with_state_dir_derives_record_and_work_dir(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", adapter_environment(), clear=True):
            state = Path(temp) / "state"
            args = build_parser().parse_args(["deploy", "--state-dir", str(state)])
            _resolve_state_paths(args)
        self.assertEqual(args.record, state / "gcp-deployment.json")
        self.assertEqual(args.work_dir, state / "gcp-work")

    def test_rollback_without_saved_inputs_explains_why(self):
        environment = adapter_environment()
        del environment["INFRAMORPH_PLAN"]
        del environment["INFRAMORPH_BUILD_ARTIFACT"]
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", environment, clear=True):
            args = build_parser().parse_args(["rollback", "--state-dir", temp])
            with self.assertRaisesRegex(AdapterError, "none were saved"):
                _resolve_state_paths(args)

    def test_successful_deploy_keeps_inputs_for_rollback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plan, artifact, _ = write_contracts(root)
            state = root / "state"
            state.mkdir()
            args = build_parser().parse_args([
                "deploy", "--plan", str(plan), "--artifact", str(artifact), "--foundation", "f",
                "--project-id", "example-project", "--deployment-id", "d", "--state-bucket", "b",
                "--state-dir", str(state),
            ])
            _save_inputs(args)
            self.assertEqual(json.loads((state / "plan.gcp.json").read_text())["target"], "gcp")
            self.assertTrue((state / "build.gcp.json").exists())

    def test_mutation_requires_execute(self):
        from adapters.gcp.cli import main

        with tempfile.TemporaryDirectory() as temp, patch("sys.stderr", new_callable=io.StringIO) as stderr, \
                patch("sys.stdout", new_callable=io.StringIO):
            plan, artifact, foundation = write_contracts(Path(temp))
            code = main([
                "deploy", "--plan", str(plan), "--artifact", str(artifact), "--foundation", str(foundation),
                "--project-id", "example-project", "--deployment-id", "d", "--state-bucket", "b",
                "--state-dir", str(Path(temp) / "state"),
            ])
        self.assertEqual(code, 2)
        self.assertIn("--execute", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
