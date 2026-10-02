import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from analyzer.local_verify import child_environment
from analyzer.source_policy import SourcePolicyError, validate_demo_plan
from adapters.aws.errors import ContractError
from control_plane.aws_config import AwsConfig, app_name, environment, owned_file
from control_plane.aws_deploy import SafeEvents, bind, check_changes, claim
from control_plane.b_bridge import DemoModules
from control_plane.db import Store
from control_plane.local_deploy import load_context
from control_plane.runtime import LocalRuntime
from schemas import RepoMap

ROOT = Path(__file__).resolve().parents[1]


class AwsBoundaries(unittest.TestCase):
    def test_source_plan_cannot_choose_cloud_identity_or_execution_settings(self):
        mapping = RepoMap.model_validate_json((ROOT / "tests/fixtures/analyzer/v1/repo_map.json").read_text())
        plan = json.loads((ROOT / "schemas/fixtures/v1/plan.aws.json").read_text())
        validate_demo_plan(plan, mapping, target="aws")
        for field, value in (("app", "other-team-app"), ("config", {"STORAGE_DRIVER": "s3", "NODE_OPTIONS": "--require=/tmp/inject.js"})):
            with self.subTest(field=field), self.assertRaises(SourcePolicyError):
                validate_demo_plan(plan | {field: value}, mapping, target="aws")
        expensive = copy.deepcopy(plan)
        expensive["services"][0].update(cpu=4096, mem=8192)
        with self.assertRaises(SourcePolicyError):
            validate_demo_plan(expensive, mapping, target="aws")
        self.assertNotEqual(app_name("p-0123456789ab"), "demo-app")
        with self.assertRaises(ValueError):
            app_name("../../demo-app")

    def test_terraform_rejects_foundation_and_data_deletion(self):
        def resource(kind, actions, address=None):
            return {"type": kind, "address": address or kind + ".test", "change": {"actions": actions}}
        check_changes({"resource_changes": [resource("aws_ecs_service", ["create"])]})
        check_changes({"resource_changes": [resource("aws_ecs_task_definition", ["create", "delete"])]})
        for item in (resource("aws_s3_bucket", ["delete"]), resource("aws_secretsmanager_secret", ["delete", "create"]),
                     resource("aws_ecs_service", ["create"], "module.foundation.aws_ecs_service.test"),
                     resource("aws_db_instance", ["update"])):
            with self.subTest(resource=item), self.assertRaises(ContractError):
                check_changes({"resource_changes": [item]})

    def test_cloud_credentials_are_not_in_analyzer_children(self):
        config = AwsConfig(foundation="/private/foundation.json", account_id="111122223333",
                           profile="test", credentials_file="/private/credentials", config_file="/private/config",
                           tools_dir="/private/bin", state_bucket="test-inframorph-state")
        aws = environment(config, child_environment())
        self.assertEqual(aws["AWS_SHARED_CREDENTIALS_FILE"], "/private/credentials")
        with patch.dict("os.environ", aws, clear=True):
            safe = child_environment()
        self.assertFalse(any(key.startswith("AWS_") for key in safe))

    def test_cloud_config_rejects_shared_permissions_and_symlinks(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            path = root / "config.json"
            path.write_text("{}")
            path.chmod(0o644)
            with self.assertRaises(ValueError): owned_file(path)
            path.chmod(0o600)
            self.assertEqual(owned_file(path), path)
            link = root / "link"; link.symlink_to(path)
            with self.assertRaises(ValueError): owned_file(link)

    def test_failure_events_are_codes_and_url_is_operator_bound(self):
        from types import SimpleNamespace
        events = SafeEvents(SimpleNamespace(deployment_id="test-1"), "https://cp-test.apps.example.com")
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            events.emit("infra", "fail", "password=private-canary")
        self.assertNotIn("private-canary", output.getvalue())
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            events.emit("smoke", "ok", url="https://cp-test.apps.example.com/health")
        self.assertEqual(json.loads(output.getvalue())["url"], "https://cp-test.apps.example.com")
        with self.assertRaises(ValueError):
            events.emit("url", "ok", url="https://attacker.example")

    def test_storage_environment_matches_actual_code_patch(self):
        template = (ROOT / "code_patch/templates/storage.js").read_text()
        terraform = (ROOT / "terraform/app/locals.tf").read_text()
        for key in ("S3_BUCKET", "AWS_REGION"):
            self.assertIn("env." + key, template)
            self.assertIn('name  = "' + key + '"' if key == "S3_BUCKET" else 'name = "' + key + '"', terraform)


class AwsRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root / "cp.db")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def prepare(self, targets):
        project = self.store.create_project("https://github.com/Team-InfraMorph/demo-app", "main", targets)
        did = self.store.begin_deploy(project["project_id"])
        with patch("control_plane.aws_config.load_config"):
            runtime = LocalRuntime(root=self.root / "runtime", b_modules=DemoModules(), aws_config=self.root / "aws.json")
        result = runtime.analyze(self.store, self.store.get_deployment(did))
        self.store.set_commit(did, result["commit_sha"])
        self.store.save_plans(did, result["plans"])
        self.store.save_initial_analysis(did, result)
        return runtime, load_context(runtime.context_file(did)), result

    def test_aws_only_analysis_has_validated_aws_plan_and_no_team_api(self):
        runtime, context, result = self.prepare(["aws"])
        self.assertEqual(set(result["plans"]), {"aws"})
        self.assertEqual(result["metrics"]["api_calls"], 0)
        self.assertEqual(context.aws_plan.app, context.intent.app)
        command = runtime.command(self.store, context.deployment_id, "aws")
        self.assertIn("control_plane.aws_deploy", command)
        self.assertIn("--aws-config", command)

    def test_aws_cannot_execute_before_approval_or_with_modified_plan(self):
        runtime, context, result = self.prepare(["aws"])
        with self.assertRaises(ValueError): bind(context, self.store)
        self.store.await_approval(context.deployment_id, ["first AWS deployment"])
        self.store.resolve_approval(context.deployment_id, True)
        bind(context, self.store)
        changed = context.aws_plan.model_copy(update={"config": {"STORAGE_DRIVER": "s3", "UNREVIEWED": "1"}})
        with self.assertRaises(ValueError): bind(context.model_copy(update={"aws_plan": changed}), self.store)

    def test_local_and_aws_claims_are_separate_and_aws_cannot_repeat(self):
        runtime, context, result = self.prepare(["local", "aws"])
        self.assertEqual(set(result["plans"]), {"local", "aws"})
        self.store.claim_runtime_run(context.deployment_id, context.repo_map.commit)
        claim(self.store, context)
        reopened = Store(self.store.path)
        try:
            with self.assertRaises(ValueError): claim(reopened, context)
        finally:
            reopened.close()

    def test_validated_retry_preserves_failed_history_and_reuses_only_reviewed_cache(self):
        runtime, context, result = self.prepare(["aws"])
        self.store.save_analysis(context.project_id, result["commit_sha"], result["repo_map"], result["intent"])
        self.store.fail(context.deployment_id)
        new_id = self.store.retry_validated_deployment(context.deployment_id)
        self.assertEqual(self.store.get_deployment(context.deployment_id)["status"], "FAILED")
        new = self.store.get_deployment(new_id)
        self.assertEqual(new["triggered_by"], "manual")
        self.assertEqual(new["analysis_mode"], "rebuild_only")
        analyzed = runtime.analyze(self.store, new)
        self.assertEqual(analyzed["metrics"]["model_calls"], 0)
        self.assertEqual(analyzed["metrics"]["api_calls"], 0)
        self.assertEqual(analyzed["intent"], result["intent"])

    def test_failed_unvalidated_analysis_cannot_use_retry(self):
        p = self.store.create_project("https://github.com/Team-InfraMorph/demo-app", "main", ["aws"])
        did = self.store.begin_deploy(p["project_id"])
        self.store.fail(did)
        with self.assertRaises(ValueError): self.store.retry_validated_deployment(did)


if __name__ == "__main__":
    unittest.main()
