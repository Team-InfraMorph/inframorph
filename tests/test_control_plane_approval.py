import hashlib
import hmac
import json
import os
import tempfile
import unittest
import warnings
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

warnings.filterwarnings("ignore", category=DeprecationWarning)

from fastapi.testclient import TestClient  # noqa: E402

from tests.cp_isolation import NO_MODULES  # noqa: E402,F401
from control_plane.app import create_app  # noqa: E402
from control_plane.change_detector import plan_diff  # noqa: E402
from control_plane.aws_config import app_name  # noqa: E402
from control_plane.aws_deploy import approved_worker_removals, check_changes  # noqa: E402
from adapters.aws.errors import ContractError  # noqa: E402
from adapters.aws.naming import AppIdentity  # noqa: E402
from adapters.aws.tests.test_aws_adapter import sample_record  # noqa: E402
from schemas import Plan  # noqa: E402

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

    def rollback_worker_context(self):
        pending = self.push_v2()
        self.client.post(f"/api/deployments/{pending}/approve")
        did = self.client.post(f"/api/deployments/{pending}/rollback").json()["deployment_id"]
        store = self.client.app.state.store
        context = SimpleNamespace(project_id=self.project, deployment_id=did,
                                  aws_plan=Plan.model_validate(store.get_plans(did)["aws"]))
        identity = AppIdentity.from_app(app_name(self.project))
        record = sample_record(deployment_id=pending, app_id=identity.app_id,
            source_revision=V2_SHA, state_key=identity.state_key,
            service_names={s["name"]: identity.resource_prefix + "-" + s["name"] for s in V2["aws"]["services"]},
            terraform_values={"services": V2["aws"]["services"]})
        return context, store, record

    def test_rollback_button_authorizes_only_the_exact_previous_worker_removal(self):
        context, store, record = self.rollback_worker_context()
        self.assertIsNone(store.get_deployment(context.deployment_id)["approved_at"])
        allowed = approved_worker_removals(context, store, record)
        self.assertEqual(len(allowed), 9)
        for address in allowed:
            check_changes({"resource_changes": [{"type": address.split(".")[0], "address": address,
                                                  "change": {"actions": ["delete"]}}]}, allowed)
        for address in ('aws_s3_bucket.uploads[0]', 'aws_secretsmanager_secret.database[0]',
                        'aws_ecs_service.service["web"]'):
            with self.subTest(address=address), self.assertRaises(ContractError):
                check_changes({"resource_changes": [{"type": address.split(".")[0], "address": address,
                                                      "change": {"actions": ["delete"]}}]}, allowed)

    def test_rollback_flag_cannot_authorize_a_different_live_record(self):
        context, store, record = self.rollback_worker_context()
        with self.assertRaisesRegex(ContractError, "base_mismatch"):
            approved_worker_removals(context, store, replace(record, deployment_id="different-live"))
        with store._lock, store._conn:
            store._conn.execute("UPDATE deployments SET rollback_of=NULL WHERE id=?", (context.deployment_id,))
        with self.assertRaisesRegex(ContractError, "base_mismatch"):
            approved_worker_removals(context, store, record)

    def test_rollback_must_restore_the_persisted_commit_and_exact_aws_plan(self):
        context, store, record = self.rollback_worker_context()
        modified = context.aws_plan.model_copy(update={"config": {"STORAGE_DRIVER": "s3", "PORT": "4000"}})
        with self.assertRaisesRegex(ContractError, "base_mismatch"):
            approved_worker_removals(SimpleNamespace(**(vars(context) | {"aws_plan": modified})), store, record)
        # Even changing both the context and the new DB plan cannot change the historical target.
        store.save_plans(context.deployment_id, store.get_plans(context.deployment_id) | {"aws": modified.model_dump(mode="json")})
        with self.assertRaisesRegex(ContractError, "base_mismatch"):
            approved_worker_removals(SimpleNamespace(**(vars(context) | {"aws_plan": modified})), store, record)
        with store._lock, store._conn:
            store._conn.execute("UPDATE deployments SET commit_sha=? WHERE id=?", ("a" * 40, context.deployment_id))
        with self.assertRaisesRegex(ContractError, "base_mismatch"):
            approved_worker_removals(context, store, record)


if __name__ == "__main__":
    unittest.main()
