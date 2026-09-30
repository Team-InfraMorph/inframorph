import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("repo_policy", Path(__file__).resolve().parents[1] / "scripts/repo_policy.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)

VALID = """## 📟 Related Issues
없음
## 🐣 Overview
협업 규칙 검사를 추가합니다.
## 📍 Key Changes
- PR 본문 검사와 로컬 훅을 추가합니다.
## ✏️ Notes
"""


class PolicyTests(unittest.TestCase):
    def test_valid_branches(self):
        for name in ("feat/devops-repo-guards", "feat/backend-api-v2"):
            with self.subTest(name=name):
                self.assertEqual(policy.validate_branch(name), [])

    def test_invalid_branches(self):
        for name in ("main", "chore/setup", "feat/login", "feat/BE-api", "feat/be-api/extra", "feat/be-api\n"):
            with self.subTest(name=name):
                self.assertTrue(policy.validate_branch(name))

    def test_valid_body_and_optional_notes(self):
        self.assertEqual(policy.validate_body(VALID), [])

    def test_real_issue(self):
        for issue in ("- closes #123", "https://github.com/example/repo/issues/123"):
            self.assertEqual(policy.validate_body(VALID.replace("없음", issue)), [])

    def test_empty_body(self):
        self.assertTrue(policy.validate_body(None))

    def test_original_template_fails(self):
        template = Path(__file__).resolve().parents[1] / ".github/PULL_REQUEST_TEMPLATE.md"
        self.assertTrue(policy.validate_body(template.read_text()))

    def test_missing_or_duplicate_heading(self):
        self.assertTrue(policy.validate_body(VALID.replace("## ✏️ Notes", "")))
        self.assertTrue(policy.validate_body(VALID + "\n## ✏️ Notes\n"))

    def test_wrong_order(self):
        self.assertTrue(policy.validate_body(VALID.replace("Related Issues", "TEMP").replace("Overview", "Related Issues").replace("TEMP", "Overview")))

    def test_comment_only_overview(self):
        self.assertTrue(policy.validate_body(VALID.replace("협업 규칙 검사를 추가합니다.", "<!-- 설명을 작성하세요. -->")))

    def test_empty_key_changes(self):
        self.assertTrue(policy.validate_body(VALID.replace("- PR 본문 검사와 로컬 훅을 추가합니다.", "- [ ]")))

    def test_placeholder(self):
        self.assertTrue(policy.validate_body(VALID.replace("없음", "- closes #이슈번호")))
        self.assertTrue(policy.validate_body(VALID.replace("- PR 본문 검사와 로컬 훅을 추가합니다.", "- [기능 1]: 예시")))

    def test_missing_issue(self):
        self.assertTrue(policy.validate_body(VALID.replace("없음", "")))


if __name__ == "__main__":
    unittest.main()
