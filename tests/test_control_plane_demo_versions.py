"""A selected demo version must pin source before analysis and retain approval."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from control_plane.app import create_app
from control_plane.b_bridge import DemoModules
from control_plane.db import ConflictError, Store
from control_plane.runtime import LocalRuntime

ROOT = Path(__file__).resolve().parents[1]
REPO = "https://github.com/Team-InfraMorph/demo-app"


class DemoVersionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.runtime = LocalRuntime(root=self.root / "runtime", b_modules=DemoModules())
        self.app = create_app(db_path=self.root / "cp.db", runtime=self.runtime)
        self.store = self.app.state.store
        self.addCleanup(self.store.close)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.project = self.store.create_project(REPO, "main", ["local"])
        self.versions = {v["id"]: v["commit_sha"] for v in DemoModules().versions()}

    def test_v2_request_analyzes_selected_source_and_waits_for_worker_approval(self):
        first = self.store.begin_deploy(self.project["project_id"])
        self.store.set_commit(first, self.versions["v1"])
        plan = json.loads((ROOT / "schemas/fixtures/v1/plan.local.json").read_text())
        self.store.save_plans(first, {"local": plan})
        self.store.set_status(first, "LIVE")
        with patch.object(self.runtime, "command", side_effect=AssertionError("must wait for approval")) as command:
            response = self.client.post(f"/api/projects/{self.project['project_id']}/deploy", json={"demo_version": "v2"})
        self.assertEqual(response.status_code, 202, response.text)
        did = response.json()["deployment_id"]
        deployed = self.store.get_deployment(did)
        self.assertEqual(deployed["commit_sha"], self.versions["v2"])
        self.assertEqual(deployed["analysis_mode"], "full_analysis")
        self.assertEqual(deployed["status"], "AWAITING_APPROVAL")
        self.assertTrue(any("worker" in reason for reason in deployed["approval_reasons"]))
        self.assertEqual([s["name"] for s in self.store.get_plans(did)["local"]["services"]], ["web", "worker"])
        context = json.loads(self.runtime.context_file(did).read_text())
        self.assertIn("src/worker.js", context["repo_map"]["tree"])
        self.assertEqual(context["metrics"]["api_calls"], 0)
        command.assert_not_called()

    def test_version_catalog_is_operator_owned_and_invalid_requests_create_no_job(self):
        self.assertEqual(self.client.get("/api/runtime").json()["demo_versions"], DemoModules().versions())
        before = self.store.list_deployments(self.project["project_id"])
        for body in ({"demo_version": "v3"}, {"demo_version": "../../v2"},
                     {"demo_version": "v2", "commit_sha": "a" * 40}):
            response = self.client.post(f"/api/projects/{self.project['project_id']}/deploy", json=body)
            self.assertEqual(response.status_code, 422)
        self.assertEqual(before, self.store.list_deployments(self.project["project_id"]))
        other = self.store.create_project("https://github.com/Team-InfraMorph/inframorph", "main", ["local"])
        response = self.client.post(f"/api/projects/{other['project_id']}/deploy", json={"demo_version": "v2"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.get_deployment(other["deployment_id"])["status"], "CREATED")

    def test_real_b_runtime_does_not_offer_or_accept_demo_version_override(self):
        runtime = LocalRuntime(root=self.root / "non-demo", b_modules=object(), replay="unused.json")
        app = create_app(db_path=self.root / "non-demo.db", runtime=runtime)
        try:
            with TestClient(app) as client:
                project = client.post("/api/projects", json={"repo_url": REPO, "targets": ["local"]}).json()
                self.assertEqual(client.get("/api/runtime").json()["demo_versions"], [])
                response = client.post(f"/api/projects/{project['project_id']}/deploy", json={"demo_version": "v2"})
                self.assertEqual(response.status_code, 409)
        finally:
            app.state.store.close()

    def test_explicit_version_never_relabels_a_queued_push_or_bypasses_active_lock(self):
        project = self.project["project_id"]
        queued = self.store.create_push_deployment(project, self.versions["v1"], "reanalyze", ["queued push"])
        did = self.store.begin_deploy(project, revision=self.versions["v2"])
        self.assertNotEqual(did, queued)
        self.assertEqual(self.store.get_deployment(queued)["status"], "SUPERSEDED")
        self.assertEqual(self.store.get_deployment(queued)["commit_sha"], self.versions["v1"])
        self.assertEqual(self.store.get_deployment(did)["commit_sha"], self.versions["v2"])
        with self.assertRaises(ConflictError):
            self.store.begin_deploy(project, revision=self.versions["v1"])
        self.store.close()
        self.store = Store(self.root / "cp.db")
        self.addCleanup(self.store.close)
        self.assertEqual(self.store.get_deployment(did)["commit_sha"], self.versions["v2"])


if __name__ == "__main__":
    unittest.main()
