import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("issue_policy", Path(__file__).resolve().parents[1] / "scripts/issue_policy.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)
VALID = "\n\n".join("### " + name + "\n" + ("- [ ] 정상 입력을 검증한다." if name == "할 일" else "공통 작업") for name in policy.SECTIONS)


class IssuePolicyTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(policy.validate_body(VALID), [])

    def test_completed_tasks_allowed(self):
        self.assertEqual(policy.validate_body(VALID.replace("[ ]", "[x]")), [])

    def test_empty(self):
        self.assertTrue(policy.validate_body(None))

    def test_missing_heading(self):
        self.assertTrue(policy.validate_body(VALID.replace("### 목표", "목표")))

    def test_duplicate_heading(self):
        self.assertTrue(policy.validate_body(VALID + "\n### 목표\n중복"))

    def test_comment_only(self):
        self.assertTrue(policy.validate_body(VALID.replace("공통 작업", "<!-- 작성 -->")))

    def test_no_response(self):
        self.assertTrue(policy.validate_body(VALID.replace("공통 작업", "_No response_")))

    def test_task_list_required(self):
        self.assertTrue(policy.validate_body(VALID.replace("- [ ]", "-")))

    def test_placeholder(self):
        self.assertTrue(policy.validate_body(VALID.replace("정상 입력을 검증한다.", "구체적인 작업을 작성하세요.")))

    @patch.dict("os.environ", {"GITHUB_REPOSITORY": "example/repo"})
    @patch.object(policy, "api")
    def test_label_invalid_issue(self, api):
        api.side_effect = [{"state": "open", "body": "", "labels": []}, None]
        self.assertEqual(policy.audit([2]), 1)
        self.assertEqual(api.call_args.args, ("/repos/example/repo/issues/2/labels", "POST", {"labels": ["needs-info"]}))

    @patch.dict("os.environ", {"GITHUB_REPOSITORY": "example/repo"})
    @patch.object(policy, "api")
    def test_remove_only_our_label_when_fixed(self, api):
        api.side_effect = [{"state": "open", "body": VALID, "labels": [{"name": "needs-info"}, {"name": "bug"}]}, None]
        self.assertEqual(policy.audit([2]), 0)
        self.assertEqual(api.call_args.args, ("/repos/example/repo/issues/2/labels/needs-info", "DELETE"))

    @patch.dict("os.environ", {"GITHUB_REPOSITORY": "example/repo"})
    @patch.object(policy, "api")
    def test_skip_closed_and_pull_requests(self, api):
        api.side_effect = [{"state": "closed"}, {"pull_request": {}}]
        self.assertEqual(policy.audit([1, 2]), 0)
        self.assertEqual(api.call_count, 2)


if __name__ == "__main__":
    unittest.main()
