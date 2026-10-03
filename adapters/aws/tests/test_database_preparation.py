import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from adapters.aws.aws_api import AwsApi
from adapters.aws.contracts import BuildArtifact, Plan
from adapters.aws.database import DatabaseReceipt
from adapters.aws.deployment import DeploymentOrchestrator, DeploymentRequest
from adapters.aws.errors import DeploymentError
from adapters.aws.tests.test_aws_adapter import artifact_data, load_foundation, plan_data, ScriptedRunner


class DatabasePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.request = DeploymentRequest("new", "bucket", "prisma db push", 30, False, root / "work", root / "record.json")
        self.aws = Mock(spec=AwsApi)
        self.aws.current_secret_version.return_value = "version-1"
        self.orchestrator = DeploymentOrchestrator(Plan.parse(plan_data()), BuildArtifact.parse(artifact_data()),
                                                  load_foundation(), terraform=Mock(), aws=self.aws)
        self.outputs = {"app_secret_arn": "secret-arn", "data_task_security_group_id": "sg-data",
                        "bootstrap_task_definition_arn": "bootstrap:1", "migration_task_definition_arn": "migration:1"}
        self.receipt = root / "database-bootstrap.json"

    def prepare(self, initialize=False):
        return self.orchestrator._prepare_database(self.outputs, self.request, initialize)

    def tasks(self):
        return [call.args[0] for call in self.aws.run_task.call_args_list]

    def test_first_runs_both_and_every_redeploy_still_runs_migration(self):
        first = self.prepare(initialize=True)
        self.assertEqual(self.tasks(), ["bootstrap:1", "migration:1"])
        self.assertEqual(first["bootstrap"], "executed")
        self.assertEqual(first["migration"], "executed")
        self.aws.run_task.reset_mock()
        second = self.prepare()
        self.assertEqual(self.tasks(), ["migration:1"])
        self.assertEqual(second["bootstrap"], "reused")
        self.assertEqual(second["migration"], "executed")
        self.assertNotIn("migration", json.loads(self.receipt.read_text()))
        self.assertEqual(self.receipt.stat().st_mode & 0o777, 0o600)

    def test_changed_migration_always_runs_without_repeating_bootstrap(self):
        self.prepare()
        self.aws.run_task.reset_mock()
        self.outputs["migration_task_definition_arn"] = "migration:2"
        self.prepare()
        self.assertEqual(self.tasks(), ["migration:2"])

    def test_secret_rotation_bootstrap_revision_and_db_replacement_invalidate_receipt(self):
        self.prepare()
        for change in (lambda: setattr(self.aws.current_secret_version, "return_value", "version-2"),
                       lambda: self.outputs.update(bootstrap_task_definition_arn="bootstrap:2"),
                       lambda: self.outputs.update(app_secret_arn="replacement-secret")):
            change()
            self.aws.run_task.reset_mock()
            result = self.prepare()
            self.assertEqual(result["bootstrap"], "executed")
            self.assertEqual(len(self.tasks()), 2)

    def test_failed_bootstrap_never_reuses_the_older_receipt(self):
        self.prepare()
        self.outputs["bootstrap_task_definition_arn"] = "bootstrap:2"
        self.aws.run_task.side_effect = DeploymentError("bootstrap failed")
        with self.assertRaises(DeploymentError): self.prepare()
        self.assertNotIn("bootstrap", json.loads(self.receipt.read_text()))
        self.outputs["bootstrap_task_definition_arn"] = "bootstrap:1"
        self.aws.run_task.side_effect = None
        self.aws.run_task.reset_mock()
        self.prepare()
        self.assertEqual(self.tasks(), ["bootstrap:1", "migration:1"])

    def test_failed_migration_is_retried_and_never_marked_completed(self):
        self.prepare()
        self.aws.run_task.side_effect = DeploymentError("schema check failed")
        with self.assertRaises(DeploymentError): self.prepare()
        self.aws.run_task.side_effect = None
        self.aws.run_task.reset_mock()
        self.prepare()
        self.assertEqual(self.tasks(), ["migration:1"])
        self.assertNotIn("migration", json.loads(self.receipt.read_text()))

    def test_missing_or_corrupt_receipt_repeats_bootstrap(self):
        self.receipt.write_text("not json")
        self.prepare()
        self.assertEqual(self.tasks(), ["bootstrap:1", "migration:1"])

    def test_secret_initialization_invalidates_any_old_bootstrap_receipt(self):
        self.prepare()
        self.aws.run_task.reset_mock()
        self.prepare(initialize=True)
        self.assertEqual(self.tasks(), ["bootstrap:1", "migration:1"])

    def test_migration_cannot_be_cached(self):
        with self.assertRaisesRegex(ValueError, "only_database_bootstrap"):
            DatabaseReceipt(self.receipt).run("migration", "a" * 64, lambda: None)

    def test_secret_metadata_is_used_without_retrieving_passwords(self):
        runner = ScriptedRunner({"describe-secret": [
            {"VersionIdsToStages": {"old": ["AWSPREVIOUS"], "current": ["AWSCURRENT"]}},
            {"VersionIdsToStages": {}},
        ]})
        aws = AwsApi(load_foundation(), runner)
        self.assertEqual(aws.current_secret_version("secret-arn"), "current")
        with self.assertRaises(DeploymentError): aws.current_secret_version("secret-arn")
        self.assertTrue(all(call[2] == "describe-secret" for call in runner.calls))


if __name__ == "__main__":
    unittest.main()
