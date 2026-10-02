from pathlib import Path
import unittest

from control_plane.change_detector import detect_changes
from schemas.intent import Intent
from schemas.repo_map import RepoMap


FIXTURES = Path(__file__).resolve().parents[1] / "schemas" / "fixtures" / "v1"


class ChangeDetectorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_map = RepoMap.model_validate_json((FIXTURES / "repo_map.json").read_text())
        cls.intent = Intent.model_validate_json((FIXTURES / "intent.json").read_text())

    def test_document_only_change_reuses_intent(self):
        decision = detect_changes(["README.md", ".github/workflows/repo-policy.yml"], self.repo_map, self.intent)
        self.assertFalse(decision.requires_analysis)
        self.assertEqual(decision.categories, ())

    def test_dependency_change_requires_analysis(self):
        decision = detect_changes(["package.json"], self.repo_map, self.intent)
        self.assertTrue(decision.requires_analysis)
        self.assertIn("dependency", decision.categories)

    def test_storage_and_schema_hints_require_analysis(self):
        decision = detect_changes(["src/images.js", "prisma/schema.prisma"], self.repo_map, self.intent)
        self.assertTrue(decision.requires_analysis)
        self.assertIn("file_write", decision.categories)
        self.assertIn("schema", decision.categories)

    def test_new_source_file_requires_analysis(self):
        decision = detect_changes(["src/worker.js"], self.repo_map, self.intent)
        self.assertTrue(decision.requires_analysis)
        self.assertIn("new_source", decision.categories)

    def test_paths_are_normalized_and_deduplicated(self):
        decision = detect_changes(["./package.json", "package.json"], self.repo_map, self.intent)
        self.assertEqual(decision.changed_files, ("package.json",))

    def test_state_evidence_change_requires_analysis(self):
        decision = detect_changes(["src/images.js"], self.repo_map.model_copy(update={"hints": []}), self.intent)
        self.assertIn("state", decision.categories)

    def test_env_template_change_requires_analysis(self):
        decision = detect_changes([".env.example"], self.repo_map, self.intent)
        self.assertIn("environment", decision.categories)

    def test_traversal_path_is_rejected(self):
        with self.assertRaises(ValueError):
            detect_changes(["../package.json"], self.repo_map, self.intent)


if __name__ == "__main__":
    unittest.main()
