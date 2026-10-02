from argparse import Namespace
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from schemas import Intent
from analyzer import local_verify as verify


class LocalVerificationTests(unittest.TestCase):
    def setUp(self):
        self.mapping, self.snapshot, self.prompt = verify.prepare("v1")
        self.expected = json.loads((verify.ROOT / "schemas/fixtures/v1/intent.json").read_text())

    def test_prompt_has_sources_but_not_reference_answer(self):
        self.assertIn('"source_files"', self.prompt)
        self.assertIn("await fs.readFile", self.prompt)
        self.assertNotIn(self.expected["state"][1]["reason"], self.prompt)
        self.assertNotIn("replay.json", self.prompt)
        self.assertNotIn("OPENAI_API_KEY", self.prompt)

    def test_environment_does_not_forward_keys_or_api_overrides(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key", "CODEX_API_KEY": "test-key",
                                     "OPENAI_BASE_URL": "https://unwanted.example",
                                     "CODEX_ACCESS_TOKEN": "test-token", "DATABASE_URL": "private"}):
            env = verify.child_environment()
        self.assertFalse({"OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL",
                          "CODEX_ACCESS_TOKEN", "DATABASE_URL"} & env.keys())
        self.assertIn("PATH", env)

    def test_comparison_allows_wording_and_citation_changes(self):
        actual = deepcopy(self.expected)
        actual["state"].reverse()
        actual["state"][0]["path"] = "uploads"
        actual["state"][0]["reason"] = "Written images are retrieved by later requests"
        actual["state"][0]["evidence"] = ["src/images.js:23"]
        intent, _ = verify.evaluate(json.dumps(actual), self.mapping, self.snapshot)
        self.assertTrue(verify.compare(intent, Intent.model_validate(self.expected))["requirements_match"])

    def test_missing_worker_and_wrong_database_are_detected(self):
        v2 = Intent.model_validate_json((verify.ROOT / "schemas/fixtures/v2/intent.json").read_text())
        actual = v2.model_copy(deep=True)
        actual.workloads.pop()
        actual.state[0].engine = "postgresql"
        comparison = verify.compare(actual, v2)
        self.assertEqual(comparison["different_fields"], ["workloads", "state"])

    def test_revision_unavailable_evidence_and_secret_outputs_are_rejected(self):
        variants = []
        wrong_revision = deepcopy(self.expected)
        wrong_revision["source_revision"] = "a" * 40
        variants.append(wrong_revision)
        for citation in (".env:1", "src/server.js:9999", "src/server.js:2"):
            invalid = deepcopy(self.expected)
            invalid["workloads"][0]["evidence"] = [citation]
            variants.append(invalid)
        secret = deepcopy(self.expected)
        secret["config"] = {"API_KEY": "sk-this-is-a-fake-key-for-testing-only"}
        variants.append(secret)
        for invalid in variants:
            with self.subTest(invalid=invalid), self.assertRaisesRegex(verify.VerificationError, "invalid_intent"):
                verify.evaluate(json.dumps(invalid), self.mapping, self.snapshot)

    def test_unknowns_and_extra_config_require_review(self):
        for extra in ({"unknowns": ["Need deletion requirements"]}, {"config": {"PORT": "9999"}},
                      {"config": {"OTHER": "value"}}):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / "input.json"
                source.write_text(json.dumps(self.expected | extra))
                args = Namespace(action="check", intent=source, compare=None)
                report = verify.verify_case("v1", args, Path(tmp) / "out")
                self.assertEqual(report["status"], "needs_review")
                self.assertEqual(report["semantic_evidence_review"], "required")

    def test_proven_optional_port_is_accepted_without_hiding_raw_difference(self):
        actual = Intent.model_validate(self.expected | {"config": {"PORT": "3000"}})
        expected = Intent.model_validate(self.expected)
        self.assertFalse(verify.compare(actual, expected)["config_match"])
        self.assertEqual(verify.config_review(actual, expected, self.snapshot),
                         {"accepted_source_defaults": ["PORT"], "unreviewed_keys": []})
        self.snapshot.files["src/server.js"] = ["const PORT = Number(process.env.PORT);"]
        self.assertEqual(verify.config_review(actual, expected, self.snapshot)["unreviewed_keys"], ["PORT"])

    def test_offline_check_compares_saved_outputs_without_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "api-intent.json"
            source.write_text(json.dumps(self.expected))
            output = Path(tmp) / "checked"
            with patch.object(verify, "run_codex", side_effect=AssertionError("Must stay offline")):
                code = verify.main(["check", "--case", "v1", "--intent", str(source),
                                    "--compare", str(source), "--output-dir", str(output)])
            self.assertEqual(code, 0)
            report = json.loads((output / "v1/report.json").read_text())
            self.assertTrue(report["comparison_to_saved_intent"]["requirements_match"])
            self.assertFalse(report["team_api_called_by_verifier"])
            self.assertIsNone(report["api_billing_usd"])

    def test_failure_does_not_save_invalid_response_or_claim_a_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = Namespace(action="run", model="test-model", timeout=10)
            with patch.object(verify, "run_codex", return_value=("private-invalid-output", {})):
                report = verify.verify_case("v1", args, Path(tmp) / "out")
            self.assertEqual(report["status"], "error")
            self.assertEqual(report["error"], "invalid_intent")
            self.assertNotIn("private-invalid-output", json.dumps(report))
            self.assertFalse((Path(tmp) / "out/intent.json").exists())

    def test_difference_from_saved_comparison_is_not_reported_as_matched(self):
        with tempfile.TemporaryDirectory() as tmp:
            actual = Path(tmp) / "actual.json"
            actual.write_text(json.dumps(self.expected))
            other = deepcopy(self.expected)
            other["state"][0]["engine"] = "postgresql"
            reference = Path(tmp) / "other.json"
            reference.write_text(json.dumps(other))
            args = Namespace(action="check", intent=actual, compare=reference)
            report = verify.verify_case("v1", args, Path(tmp) / "out")
            self.assertEqual(report["status"], "mismatch")
            self.assertEqual(report["comparison_to_saved_intent"]["different_fields"], ["state"])

    def test_api_key_login_is_rejected_before_model_execution(self):
        result = Namespace(returncode=0, stdout="Logged in using an API key", stderr="")
        with patch.object(verify.shutil, "which", return_value="codex"), \
                patch.object(verify.subprocess, "run", return_value=result), \
                patch.object(verify.subprocess, "Popen") as model:
            with self.assertRaisesRegex(verify.VerificationError, "chatgpt_login_required"):
                verify.run_codex(self.prompt, "test-model", 5)
            model.assert_not_called()

    def test_existing_output_directory_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "summary.json"
            marker.write_text("previous result")
            with patch.object(verify, "run_codex") as model:
                self.assertEqual(verify.main(["run", "--output-dir", tmp]), 1)
                model.assert_not_called()
            self.assertEqual(marker.read_text(), "previous result")


if __name__ == "__main__":
    unittest.main()
