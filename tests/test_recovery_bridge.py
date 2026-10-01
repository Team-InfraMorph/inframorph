"""C subprocess contracts used by the D/E connection; no API or Docker calls."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from analyzer.feedback import AnalysisFeedback, LocalFailure, PatchReference
from analyzer.recovery_smoke import read_context
from code_patch import patch_snapshot
from code_patch.local_smoke import environment
from schemas import Intent, Plan, RepoMap

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/analyzer/v1"


class RecoveryBridgeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.work = Path(tmp.name).resolve()
        self.mapping = RepoMap.model_validate_json((FIXTURE / "repo_map.json").read_text())
        self.intent = Intent.model_validate_json((ROOT / "schemas/fixtures/v1/intent.json").read_text())
        self.plan = Plan.model_validate_json((ROOT / "schemas/fixtures/v1/plan.local.json").read_text())
        self.context = {"repo_map": self.mapping.model_dump(mode="json"),
                        "intent": self.intent.model_dump(mode="json"), "plan": self.plan.model_dump(mode="json")}
        self.context_file = self.work / "context.json"

    def write_context(self, data):
        self.context_file.write_text(json.dumps(data))
        return self.context_file

    def test_control_plane_context_accepts_reviewed_contract_and_source_port(self):
        context = read_context(self.write_context(self.context), "v1")
        self.assertEqual(context.intent.source_revision, self.mapping.commit)
        extra = deepcopy(self.context)
        extra["intent"]["config"]["PORT"] = "3000"
        self.assertEqual(read_context(self.write_context(extra), "v1").intent.config["PORT"], "3000")

    def test_changed_commit_or_plan_is_rejected_before_docker(self):
        for section, field, value in (("repo_map", "commit", "0" * 40), ("plan", "config", {"STORAGE_DRIVER": "fs", "PORT": "3100"})):
            with self.subTest(section=section):
                data = deepcopy(self.context)
                data[section][field] = value
                with self.assertRaisesRegex(ValueError, "unreviewed_pipeline_context"):
                    read_context(self.write_context(data), "v1")

    def test_unknowns_and_invented_evidence_do_not_authorize_execution(self):
        data = deepcopy(self.context)
        data["intent"]["unknowns"] = ["unresolved health"]
        with self.assertRaisesRegex(ValueError, "unreviewed_pipeline_intent"):
            read_context(self.write_context(data), "v1")
        data = deepcopy(self.context)
        data["intent"]["workloads"][0]["evidence"] = ["src/server.js:99999"]
        with self.assertRaises(ValueError):
            read_context(self.write_context(data), "v1")

    def test_secret_or_oversize_context_is_rejected(self):
        data = deepcopy(self.context)
        data["plan"]["config"]["API_KEY"] = "fake-private-key-value-123456789"
        with self.assertRaisesRegex(ValueError, "unsafe_pipeline_context"):
            read_context(self.write_context(data), "v1")
        self.context_file.write_text(" " * 120_001)
        with self.assertRaisesRegex(ValueError, "unsafe_pipeline_context"):
            read_context(self.context_file, "v1")

    def cli(self, feedback):
        file = self.work / "feedback.json"
        file.write_text(feedback)
        return subprocess.run([sys.executable, "-m", "analyzer", "--repo-map", str(FIXTURE / "repo_map.json"),
            "--snapshot", str(FIXTURE / "snapshot"), "--replay", str(FIXTURE / "replay.json"),
            "--feedback", str(file)], cwd=ROOT, env=environment(), capture_output=True, text=True, timeout=10)

    def test_analyzer_feedback_cli_preserves_intent_and_metrics_protocol(self):
        manifest = patch_snapshot(FIXTURE / "snapshot", self.mapping, self.plan, self.work / "initial")
        feedback = AnalysisFeedback(failure=LocalFailure(stage="health", code="port_mismatch",
            log="expected 3000. API_KEY='fake-private-key-value-123456789'"), previous_intent=self.intent,
            previous_plan=self.plan, previous_patch=PatchReference.from_manifest(manifest))
        result = self.cli(feedback.model_dump_json())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Intent.model_validate_json(result.stdout), self.intent)
        self.assertEqual(json.loads(result.stderr)["metrics"]["api_calls"], 0)
        self.assertNotIn("fake-private-key-value", result.stdout + result.stderr)

    def test_invalid_feedback_cli_emits_safe_error_and_no_intent(self):
        for data in ("not JSON fake-private-key-value-123456789", " " * 120_001):
            with self.subTest(size=len(data)):
                result = self.cli(data)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(json.loads(result.stderr)["error"], "invalid_cli_input")
                self.assertNotIn("fake-private-key-value", result.stderr)

    def test_deployment_id_cannot_combine_multiple_fixtures_in_one_stream(self):
        result = subprocess.run([sys.executable, "-m", "analyzer.recovery_smoke", "--case", "all",
            "--deployment-id", "d-test"], cwd=ROOT, env=environment(), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
