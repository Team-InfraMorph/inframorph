import hashlib
import hmac
import json
import os
import tempfile
import unittest
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=DeprecationWarning)

from fastapi.testclient import TestClient  # noqa: E402

from tests.cp_isolation import NO_MODULES  # noqa: E402,F401
from control_plane.app import create_app  # noqa: E402
from control_plane.change_detector import plan_diff  # noqa: E402

SECRET = "test-secret"
REPO = "https://github.com/Team-InfraMorph/demo-app"
FIXTURES = Path(__file__).resolve().parents[1] / "schemas" / "fixtures"


def plans(version):
    return {t: json.loads((FIXTURES / version / f"plan.{t}.json").read_text()) for t in ("local", "aws")}


V1, V2 = plans("v1"), plans("v2")
V1_SHA, V2_SHA = V1["aws"]["source_revision"], V2["aws"]["source_revision"]


class PlanDiffTest(unittest.TestCase):
    def test_new_worker_is_an_infra_change_on_both_targets(self):
        self.assertEqual(plan_diff(V1, V2), ["aws: 서비스 worker 추가 (worker)", "local: 서비스 worker 추가 (worker)"])

    def test_same_structure_or_first_deploy_needs_no_approval(self):
        self.assertEqual(plan_diff(V2, V2), [])
        self.assertEqual(plan_diff({}, V2), [])

    def test_db_and_storage_type_changes_are_reported(self):
        changed = json.loads(json.dumps(V1))
        changed["aws"]["storage"] = None
        self.assertEqual(plan_diff(V1, changed), ["aws: storage s3 → 없음"])


class ApprovalAndRollbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["GITHUB_WEBHOOK_SECRET"] = SECRET
        os.environ["INFRAMORPH_FAKE_DELAY"] = "0"
        self.client = TestClient(create_app(db_path=Path(self.tmp.name) / "cp.db"))
        self.project = self.client.post(
            "/api/projects", json={"repo_url": REPO, "targets": ["local", "aws"]}).json()["project_id"]
        self.first = self.client.post(f"/api/projects/{self.project}/deploy").json()["deployment_id"]

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()
        os.environ.pop("GITHUB_WEBHOOK_SECRET", None)
        os.environ.pop("INFRAMORPH_FAKE_DELAY", None)

    def get(self, deployment_id):
        return self.client.get(f"/api/deployments/{deployment_id}").json()

    def push_v2(self, delivery="d1"):
        body = json.dumps({"ref": "refs/heads/main", "before": V1_SHA, "after": V2_SHA,
                           "commits": [{"added": ["src/worker.js"], "modified": [], "removed": []}],
                           "repository": {"html_url": REPO}}).encode()
        signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        res = self.client.post("/api/webhooks/github", content=body, headers={
            "X-GitHub-Event": "push", "X-GitHub-Delivery": delivery, "X-Hub-Signature-256": signature})
        return res.json()["redeploys"][0]["deployment_id"]

    def test_first_deploy_caches_analysis_so_text_push_skips_ai(self):
        self.assertEqual(self.get(self.first)["commit_sha"], V1_SHA)
        body = json.dumps({"ref": "refs/heads/main", "before": V1_SHA, "after": "c" * 40,
                           "commits": [{"added": [], "modified": ["README.md"], "removed": []}],
                           "repository": {"html_url": REPO}}).encode()
        signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        res = self.client.post("/api/webhooks/github", content=body, headers={
            "X-GitHub-Event": "push", "X-GitHub-Delivery": "t1", "X-Hub-Signature-256": signature})
        item = res.json()["redeploys"][0]
        self.assertEqual(item["mode"], "rebuild_only")
        self.assertEqual(self.get(item["deployment_id"])["status"], "LIVE")

    def test_worker_added_waits_for_approval_then_deploys(self):
        self.assertEqual(self.get(self.first)["status"], "LIVE")
        pending = self.push_v2()
        deployment = self.get(pending)
        self.assertEqual(deployment["status"], "AWAITING_APPROVAL")
        self.assertIn("aws: 서비스 worker 추가 (worker)", deployment["approval_reasons"])
        self.assertEqual(deployment["targets"]["aws"]["status"], "AWAITING_APPROVAL")
        self.assertEqual(set(self.client.get(f"/api/deployments/{pending}/plans").json()), {"local", "aws"})

        self.assertEqual(self.client.post(f"/api/deployments/{pending}/approve").status_code, 202)
        deployment = self.get(pending)
        self.assertEqual(deployment["status"], "LIVE")
        self.assertIsNotNone(deployment["approved_at"])
        self.assertEqual(self.client.post(f"/api/deployments/{pending}/approve").status_code, 409)

    def test_approval_compares_with_last_live_that_has_plans(self):
        self.test_first_deploy_caches_analysis_so_text_push_skips_ai()  # 사이에 plan 없는 LIVE가 끼어 있다
        self.assertEqual(self.get(self.push_v2())["status"], "AWAITING_APPROVAL")

    def test_push_during_approval_is_queued_and_reject_fails(self):
        pending = self.push_v2()
        queued = self.push_v2("d2")
        self.assertEqual(self.get(queued)["status"], "CREATED")
        self.assertEqual(self.client.post(f"/api/deployments/{pending}/reject").json()["status"], "FAILED")

    def test_rollback_redeploys_previous_live_plans(self):
        pending = self.push_v2()
        self.client.post(f"/api/deployments/{pending}/approve")
        res = self.client.post(f"/api/deployments/{pending}/rollback")
        self.assertEqual(res.status_code, 202)
        rolled = self.get(res.json()["deployment_id"])
        self.assertEqual(rolled["triggered_by"], "rollback")
        self.assertEqual(rolled["rollback_of"], pending)
        self.assertEqual(rolled["status"], "LIVE")
        aws_plan = self.client.get(f"/api/deployments/{rolled['id']}/plans").json()["aws"]
        self.assertEqual([s["name"] for s in aws_plan["services"]], ["web"])

    def test_rollback_skips_live_deployments_that_had_no_plans(self):
        self.test_first_deploy_caches_analysis_so_text_push_skips_ai()  # v1 다음에 설계도 없는 LIVE가 끼어 있다
        pending = self.push_v2()
        self.client.post(f"/api/deployments/{pending}/approve")
        rolled = self.get(self.client.post(f"/api/deployments/{pending}/rollback").json()["deployment_id"])
        self.assertEqual(rolled["commit_sha"], V1_SHA)

    def test_rollback_without_earlier_live_is_conflict(self):
        self.assertEqual(self.client.post(f"/api/deployments/{self.first}/rollback").status_code, 409)


if __name__ == "__main__":
    unittest.main()
