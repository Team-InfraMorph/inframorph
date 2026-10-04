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
from analyzer.backend import ReplayBackend, Reply
from analyzer.source_policy import validate_demo_plan
from policy_gate.gate import PolicyError, validate_plan
from schemas import Intent, RepoMap

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
        for body in ({"demo_version": "unregistered"}, {"demo_version": "../../v2"},
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

    def test_reviewed_port_survives_demo_planning_for_both_versions_and_targets(self):
        for version in ("v1", "v2"):
            intent = Intent.model_validate_json((ROOT / f"schemas/fixtures/{version}/intent.json").read_text())
            mapping = RepoMap.model_validate_json((ROOT / f"tests/fixtures/analyzer/{version}/repo_map.json").read_text())
            for config in ({}, {"PORT": "3000"}):
                intent.config = config
                for target in ("local", "aws"):
                    with self.subTest(version=version, config=config, target=target):
                        plan = DemoModules().plan(intent, target)
                        validate_demo_plan(plan, mapping, target=target)
                        validate_plan(intent, plan)
                        self.assertEqual(plan.config.get("PORT"), config.get("PORT"))
                        self.assertEqual(plan.config["STORAGE_DRIVER"], "fs" if target == "local" else "s3")
            for config in ({"PORT": "3001"}, {"STORAGE_DRIVER": "injected"}, {"NODE_OPTIONS": "injected"}):
                intent.config = config
                with self.assertRaisesRegex(ValueError, "unsupported_demo_config"):
                    DemoModules().plan(intent)

    def port_backend(self, version):
        replies = json.loads((ROOT / f"tests/fixtures/analyzer/{version}/replay.json").read_text())
        intent = json.loads((ROOT / f"schemas/fixtures/{version}/intent.json").read_text())
        intent["config"] = {"PORT": "3000"}
        return ReplayBackend([Reply(**item) for item in replies[:-1] + [{"text": json.dumps(intent)}]])

    def test_runtime_accepts_reviewed_port_for_simultaneous_local_and_aws_plans(self):
        project = self.store.create_project(REPO, "main", ["local", "aws"])
        # Analysis only: no adapter command or AWS configuration is read.
        self.runtime.aws_config = self.root / "unused-aws.json"
        for version in ("v1", "v2"):
            with self.subTest(version=version):
                did = self.store.begin_deploy(project["project_id"], revision=self.versions[version])
                with patch("control_plane.runtime.analysis_backend", return_value=self.port_backend(version)):
                    result = self.runtime.analyze(self.store, self.store.get_deployment(did))
                self.assertEqual(set(result["plans"]), {"local", "aws"})
                for plan in result["plans"].values():
                    self.assertEqual(plan["config"]["PORT"], "3000")
                self.assertGreater(result["metrics"]["model_calls"], 0)
                self.assertEqual(result["metrics"]["api_calls"], 0)
                self.store.set_status(did, "LIVE")

    def test_api_reports_plan_failure_without_deploying_or_leaking_exception_text(self):
        original_plan = self.runtime.b.plan

        def drop_port(intent, target="local"):
            plan = original_plan(intent, target)
            plan.config.pop("PORT", None)
            return plan

        for cause in ("missing_port", "private-error-canary"):
            with self.subTest(cause=cause):
                expected = "plan_config_mismatch" if cause == "missing_port" else "policy_gate_failed"
                with patch("control_plane.runtime.analysis_backend", return_value=self.port_backend("v2")), \
                     patch.object(self.runtime.b, "plan", side_effect=drop_port), \
                     patch.object(self.runtime, "command", side_effect=AssertionError("must not deploy")) as command:
                    if cause == "missing_port":
                        response = self.client.post(f"/api/projects/{self.project['project_id']}/deploy", json={"demo_version": "v2"})
                    else:
                        with patch("control_plane.runtime.validate_plan", side_effect=PolicyError(cause)):
                            response = self.client.post(f"/api/projects/{self.project['project_id']}/deploy", json={"demo_version": "v2"})
                self.assertEqual(response.status_code, 202, response.text)
                did = response.json()["deployment_id"]
                deployment = self.store.get_deployment(did)
                self.assertEqual(deployment["status"], "FAILED")
                self.assertEqual(deployment["analysis_metrics"]["error"], expected)
                self.assertEqual(deployment["analysis_metrics"]["blocked_stage"], "plan_policy")
                events = [item["event"] for item in self.store.list_events(did)]
                self.assertTrue(any(e["step"] == "plan" and e["status"] == "fail" and
                                    e["detail"] == f"배포 설계 검사 실패: {expected}" for e in events))
                self.assertNotIn("private-error-canary", json.dumps(deployment) + json.dumps(events))
                self.assertFalse(self.runtime.context_file(did).exists())
                self.assertEqual(self.store.get_plans(did), {})
                command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
