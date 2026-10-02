import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from analyzer.control_plane_injection_verify import ALLOWED, CASES, verify
from analyzer.source_policy import SourcePolicyError
from control_plane.app import create_app
from control_plane.b_bridge import DemoModules
from control_plane.runtime import LocalRuntime


class DeploymentInjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def cases(self, *names):
        summary = verify(self.root / "check", cases=names)
        self.assertEqual(summary["status"], "passed", summary["results"])
        self.assertFalse(summary["team_api_called"] or summary["new_model_inference"] or summary["docker_called"])
        return {report["case"]: report for report in summary["results"]}

    def test_document_and_metadata_attacks_cannot_claim_human_approval(self):
        for report in self.cases("documents", "metadata").values():
            self.assertEqual(report["actual_status"], "AWAITING_APPROVAL")
            self.assertIn("attack_delivered_as_untrusted_tool_data", report["checks"])
            self.assertIn("real_e_patch_gate_passed", report["checks"])
            self.assertEqual(report["deployment_command_calls"], 0)

    def test_changed_runtime_comment_and_unmapped_code_stop_before_planner(self):
        for name, report in self.cases("source_comment", "unmapped_code").items():
            self.assertEqual(report["actual_status"], "FAILED")
            self.assertEqual(report["metrics"]["blocked_stage"], "intent_policy")
            self.assertEqual(report["metrics"]["error"],
                             "unreviewed_runtime_source" if name == "source_comment" else "local_pipeline_analysis_failed")
            self.assertEqual(report["metrics"].get("policy_fields", []), [])
            self.assertGreater(report["metrics"]["model_calls"], 0)
            self.assertIn("no_rejected_intent_plan_context_or_cache", report["checks"])

    def test_schema_valid_injected_operational_fields_never_reach_planner(self):
        names = ("intent_port", "intent_health", "intent_worker", "intent_storage", "intent_config", "intent_secret", "intent_evidence")
        fields = {"intent_port": "workloads", "intent_health": "workloads", "intent_worker": "workloads",
                  "intent_storage": "state", "intent_config": "config", "intent_secret": "secrets"}
        for name, report in self.cases(*names).items():
            self.assertEqual(report["metrics"]["blocked_stage"], "intent_policy")
            self.assertEqual(report["metrics"]["error"],
                             "non_source_evidence" if name == "intent_evidence" else "intent_source_mismatch")
            self.assertEqual(report["metrics"]["policy_fields"], [] if name == "intent_evidence" else [fields[name]])
            self.assertEqual(report["metrics"]["validation_retries"], 0)
            self.assertGreater(report["metrics"]["model_calls"], 0)
            self.assertIn("no_planner_after_intent_rejection", report["checks"])

    def test_wrong_revision_and_shell_tool_are_rejected_by_actual_c_runner(self):
        reports = self.cases("intent_revision", "tool_shell")
        self.assertEqual(reports["intent_revision"]["metrics"]["error"], "invalid_intent")
        self.assertEqual(reports["intent_revision"]["metrics"]["validation_retries"], 1)
        self.assertEqual(reports["tool_shell"]["metrics"]["error"], "tool_not_allowed")
        for report in reports.values():
            self.assertEqual(report["metrics"]["blocked_stage"], "analyzer")

    def test_planner_injection_keeps_consumed_analysis_usage_without_publishing_artifacts(self):
        report = self.cases("planner_config")["planner_config"]
        self.assertEqual(report["metrics"]["blocked_stage"], "plan_policy")
        self.assertEqual(report["metrics"]["error"], "plan_source_mismatch")
        self.assertGreater(report["metrics"]["model_calls"], 0)
        self.assertGreater(report["metrics"]["tool_calls"], 0)
        self.assertIn("no_rejected_intent_plan_context_or_cache", report["checks"])

    def test_private_file_requests_do_not_leak_canary_or_invalidate_normal_requirements(self):
        for report in self.cases("read_outside", "read_secret").values():
            self.assertEqual(report["actual_status"], "AWAITING_APPROVAL")
            self.assertIn("private_canary_not_disclosed", report["checks"])
            self.assertIn("unavailable_read_rejected", report["checks"])

    def test_cached_intent_is_rechecked_without_new_model_inference(self):
        report = self.cases("cached_intent")["cached_intent"]
        self.assertEqual(report["actual_status"], "FAILED")
        self.assertEqual(report["metrics"]["model_calls"], 0)
        self.assertEqual(report["metrics"]["blocked_stage"], "intent_policy")

    def test_case_paths_and_previous_output_cannot_be_overwritten(self):
        for names in ((), ("../outside",), ("documents", "documents")):
            with self.subTest(names=names), self.assertRaisesRegex(ValueError, "invalid_injection_cases"):
                verify(self.root / "invalid", cases=names)
        output = self.root / "existing"
        output.mkdir()
        with self.assertRaises(FileExistsError):
            verify(output, cases=("documents",))
        linked = self.root / "linked"
        linked.symlink_to(output, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "injection_output_symlink"):
            verify(linked / "new", cases=("documents",))
        self.assertEqual(len(CASES), 17)
        self.assertEqual(ALLOWED, {"documents", "metadata", "read_outside", "read_secret"})

    def test_post_analysis_e_gate_and_planner_exceptions_preserve_usage_and_hide_raw_errors(self):
        for stage, name in (("intent_gate", "control_plane.runtime.validate_intent"),
                            ("planner", "control_plane.b_bridge.DemoModules.plan")):
            runtime = LocalRuntime(root=self.root / stage / "runtime", b_modules=DemoModules())
            app = create_app(db_path=self.root / stage / "cp.db", runtime=runtime)
            store = app.state.store
            try:
                with TestClient(app) as client:
                    project = client.post("/api/projects", json={"repo_url": "https://github.com/Team-InfraMorph/demo-app",
                        "branch": "main", "targets": ["local"]}).json()["project_id"]
                    with patch(name, side_effect=ValueError("private-provider-error-canary")):
                        did = client.post(f"/api/projects/{project}/deploy").json()["deployment_id"]
                    deployment = client.get(f"/api/deployments/{did}").json()
                    analysis = client.get(f"/api/deployments/{did}/analysis").json()
                    self.assertEqual(deployment["status"], "FAILED")
                    self.assertEqual(analysis["metrics"]["model_calls"], 5)
                    self.assertEqual(analysis["metrics"]["api_calls"], 0)
                    self.assertEqual(analysis["metrics"]["blocked_stage"], stage)
                    self.assertIsNone(analysis["intent"])
                    self.assertNotIn("private-provider-error-canary", json.dumps([deployment, analysis]))
                    self.assertEqual(client.get(f"/api/deployments/{did}/plans").json(), {})
                    self.assertFalse(runtime.context_file(did).exists())
            finally:
                store.close()

    def test_policy_diagnostics_allow_only_host_codes_and_field_names(self):
        canary = "private-policy-error-canary"
        errors = (SourcePolicyError("intent_source_mismatch", ("config", canary)),
                  SourcePolicyError(canary, (canary,)))
        for index, error in enumerate(errors):
            runtime = LocalRuntime(root=self.root / str(index) / "runtime", b_modules=DemoModules())
            app = create_app(db_path=self.root / str(index) / "cp.db", runtime=runtime)
            try:
                with TestClient(app) as client:
                    project = client.post("/api/projects", json={"repo_url": "https://github.com/Team-InfraMorph/demo-app",
                        "branch": "main", "targets": ["local"]}).json()["project_id"]
                    with patch("control_plane.runtime.validate_demo_intent", side_effect=error), \
                         patch("control_plane.b_bridge.DemoModules.plan") as planner:
                        did = client.post(f"/api/projects/{project}/deploy").json()["deployment_id"]
                    planner.assert_not_called()
                    deployment = client.get(f"/api/deployments/{did}").json()
                    analysis = client.get(f"/api/deployments/{did}/analysis").json()
                    self.assertEqual(deployment["status"], "FAILED")
                    self.assertEqual(analysis["metrics"]["error"], error.code)
                    self.assertEqual(analysis["metrics"]["policy_fields"], ["config"] if index == 0 else [])
                    self.assertEqual(analysis["metrics"]["blocked_stage"], "intent_policy")
                    self.assertEqual(analysis["metrics"]["model_calls"], 5)
                    self.assertEqual(analysis["metrics"]["api_calls"], 0)
                    self.assertIsNone(analysis["intent"])
                    self.assertNotIn(canary, json.dumps([deployment, analysis]))
                    self.assertEqual(client.get(f"/api/deployments/{did}/plans").json(), {})
                    self.assertFalse(runtime.context_file(did).exists())
                    revision = json.loads((Path(__file__).resolve().parents[1] /
                        "tests/fixtures/analyzer/v1/repo_map.json").read_text())["commit"]
                    self.assertIsNone(app.state.store.get_analysis(project, revision))
            finally:
                app.state.store.close()


if __name__ == "__main__":
    unittest.main()
