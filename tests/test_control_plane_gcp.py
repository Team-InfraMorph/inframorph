import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from analyzer.local_verify import child_environment
from control_plane.b_bridge import DemoModules
from control_plane.db import Store
from control_plane.gcp_config import GcpConfig, app_name, environment, owned_directory, owned_file
from control_plane.gcp_deploy import SafeEvents, bind, claim
from control_plane.gcp_deploy import check_changes
from adapters.gcp.errors import ContractError
from control_plane.local_deploy import load_context
from control_plane.runtime import LocalRuntime


class GcpBoundaries(unittest.TestCase):
    def test_terraform_preview_allows_only_nondestructive_app_resources(self):
        def resource(kind, actions, address=None):
            return {"type": kind, "address": address or kind + ".test", "change": {"actions": actions}}
        check_changes({"resource_changes": [resource("google_cloud_run_v2_service", ["create"])]})
        # A job left tainted by a failed create is replaced on retry.
        check_changes({"resource_changes": [resource("google_cloud_run_v2_job", ["delete", "create"])]})
        for item in (
            resource("google_cloud_run_v2_job", ["delete"]),
            resource("google_secret_manager_secret", ["delete", "create"]),
            resource("google_storage_bucket", ["delete"]),
            resource("google_cloud_run_v2_service", ["delete", "create"]),
            resource("google_cloud_run_v2_service", ["create"], "module.foundation.google_cloud_run_v2_service.test"),
            resource("google_compute_network", ["update"]),
        ):
            with self.subTest(resource=item), self.assertRaises(ContractError):
                check_changes({"resource_changes": [item]})

    def test_identity_and_credentials_are_operator_owned(self):
        foundation = SimpleNamespace(deployer_service_account_email="deployer@example-project.iam.gserviceaccount.com")
        config = GcpConfig(
            foundation="/private/foundation.json", project_id="example-project",
            gcloud_config_dir="/private/gcloud", tools_dir="/private/bin",
            state_bucket="inframorph-gcp-state",
        )
        with patch("control_plane.gcp_config.FoundationOutputs.from_file", return_value=foundation):
            values = environment(config, child_environment())
        self.assertEqual(values["CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT"], foundation.deployer_service_account_email)
        self.assertEqual(values["GOOGLE_IMPERSONATE_SERVICE_ACCOUNT"], foundation.deployer_service_account_email)
        self.assertEqual(values["CLOUDSDK_CORE_PROJECT"], "example-project")
        with patch.dict(os.environ, values, clear=True):
            safe = child_environment()
        self.assertFalse(any(key.startswith(("CLOUDSDK_", "GOOGLE_")) for key in safe))
        self.assertEqual(app_name("p-0123456789ab"), "cp-p-0123456789ab")
        with self.assertRaises(ValueError):
            app_name("../../other")

    def test_config_paths_reject_shared_files_and_symlinks(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name).resolve()
            path = root / "config.json"
            path.write_text("{}")
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                owned_file(path)
            path.chmod(0o600)
            self.assertEqual(owned_file(path), path)
            root.chmod(0o700)
            self.assertEqual(owned_directory(root), root)
            link = root / "link"
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                owned_file(link)

    def test_failure_events_are_fixed_codes_and_urls_are_bound(self):
        events = SafeEvents(SimpleNamespace(deployment_id="test-1"), "https://cp-test.gcp.luckyfor.cc")
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            events.emit("infra", "fail", "password=private-canary")
        self.assertNotIn("private-canary", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["detail"], "gcp_adapter_failed")
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            events.emit("smoke", "ok", url="https://cp-test.gcp.luckyfor.cc/health")
        self.assertEqual(json.loads(output.getvalue())["url"], "https://cp-test.gcp.luckyfor.cc")
        with self.assertRaises(ValueError):
            events.emit("url", "ok", url="https://attacker.example")
        run_app = SafeEvents(SimpleNamespace(deployment_id="test-2"))
        with patch("sys.stdout", new_callable=io.StringIO):
            run_app.emit("url", "ok", url="https://service-abc-uc.a.run.app")


class GcpRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root / "cp.db")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def prepare(self, targets):
        project = self.store.create_project("https://github.com/Team-InfraMorph/demo-app", "main", targets)
        deployment_id = self.store.begin_deploy(project["project_id"])
        with patch("control_plane.gcp_config.load_config"):
            runtime = LocalRuntime(
                root=self.root / "runtime", b_modules=DemoModules(),
                gcp_config=self.root / "gcp.json",
            )
        result = runtime.analyze(self.store, self.store.get_deployment(deployment_id))
        self.store.set_commit(deployment_id, result["commit_sha"])
        self.store.save_plans(deployment_id, result["plans"])
        self.store.save_initial_analysis(deployment_id, result)
        return runtime, load_context(runtime.context_file(deployment_id)), result

    def test_gcp_analysis_and_command_use_validated_plan_and_private_config(self):
        runtime, context, result = self.prepare(["gcp"])
        self.assertEqual(set(result["plans"]), {"gcp"})
        self.assertEqual(context.gcp_plan.target.value, "gcp")
        command = runtime.command(self.store, context.deployment_id, "gcp")
        self.assertIn("control_plane.gcp_deploy", command)
        self.assertIn("--gcp-config", command)

    def test_gcp_requires_first_deployment_approval_and_single_claim(self):
        _, context, _ = self.prepare(["gcp"])
        with self.assertRaises(ValueError):
            bind(context, self.store)
        self.store.await_approval(context.deployment_id, ["first GCP deployment"])
        self.store.resolve_approval(context.deployment_id, True)
        bind(context, self.store)
        claim(self.store, context)
        with self.assertRaises(ValueError):
            claim(self.store, context)


if __name__ == "__main__":
    unittest.main()
