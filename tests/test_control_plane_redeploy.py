import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import unittest
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=DeprecationWarning)

from fastapi.testclient import TestClient  # noqa: E402

from control_plane.app import create_app, fake_deployer_cmd  # noqa: E402
from control_plane.change_detector import plan_redeploy  # noqa: E402
from control_plane.db import Store  # noqa: E402
from control_plane.webhook import PushEvent  # noqa: E402

SECRET = "test-secret"
REPO = "https://github.com/Team-InfraMorph/demo-app"
V1 = Path(__file__).resolve().parents[1] / "schemas" / "fixtures" / "v1"
V1_REPO_MAP = json.loads((V1 / "repo_map.json").read_text())
V1_INTENT = json.loads((V1 / "intent.json").read_text())
BEFORE = V1_REPO_MAP["commit"]
AFTER = "c" * 40


def push(files=(), before=BEFORE, forced=False, commits=None):
    if commits is None:
        commits = [{"added": [], "modified": list(files), "removed": []}]
    return PushEvent(repo_key=REPO.lower(), branch="main", before=before, after=AFTER,
                     changed_files=tuple(files), forced=forced, commit_count=len(commits))


class PlanRedeployTest(unittest.TestCase):
    cached = {"repo_map": V1_REPO_MAP, "intent": V1_INTENT}

    def test_text_change_with_cache_rebuilds_only(self):
        decision = plan_redeploy(push(["README.md"]), self.cached)
        self.assertEqual(decision.mode, "rebuild_only")

    def test_dependency_change_reanalyzes(self):
        decision = plan_redeploy(push(["package.json"]), self.cached)
        self.assertEqual(decision.mode, "reanalyze")
        self.assertIn("dependency", decision.categories)

    def test_unreliable_pushes_fall_back_to_full_analysis(self):
        cases = {
            "no cache": (push(["README.md"]), None),
            "forced": (push(["README.md"], forced=True), self.cached),
            "new branch": (push(["README.md"], before="0" * 40), self.cached),
            "no file list": (push([], commits=[]), self.cached),
        }
        for name, (event, cached) in cases.items():
            with self.subTest(name):
                self.assertEqual(plan_redeploy(event, cached).mode, "full_analysis")


class AnalysisCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "cp.db")
        self.project = self.store.create_project(REPO, "main", ["local"])["project_id"]

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_round_trip(self):
        self.store.save_analysis(self.project, BEFORE, V1_REPO_MAP, V1_INTENT)
        cached = self.store.get_analysis(self.project, BEFORE)
        self.assertEqual(cached["repo_map"]["commit"], BEFORE)
        self.assertEqual(cached["intent"]["source_revision"], BEFORE)
        self.assertIsNone(self.store.get_analysis(self.project, AFTER))

    def test_rejects_invalid_or_mismatched_artifacts(self):
        with self.assertRaises(ValueError):
            self.store.save_analysis(self.project, AFTER, V1_REPO_MAP, V1_INTENT)
        broken = dict(V1_INTENT, unexpected=True)
        with self.assertRaises(ValueError):
            self.store.save_analysis(self.project, BEFORE, V1_REPO_MAP, broken)

    def test_newest_queued_request_runs_and_older_ones_are_superseded(self):
        first = self.store.list_deployments(self.project)[0]["id"]
        newer = self.store.create_push_deployment(self.project, AFTER, "rebuild_only", [])
        self.assertEqual(self.store.begin_deploy(self.project), newer)
        self.assertEqual(self.store.get_deployment(first)["status"], "SUPERSEDED")
        self.assertEqual(self.store.get_deployment(newer)["targets"], {"local": {"status": "DEPLOYING", "url": None}})
        self.assertFalse(self.store.has_queued(self.project))

    def test_old_database_gets_new_columns(self):
        old = Path(self.tmp.name) / "old.db"
        with sqlite3.connect(old) as conn:
            conn.execute("CREATE TABLE deployments (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
                         "status TEXT NOT NULL, commit_sha TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
        Store(old).close()
        with sqlite3.connect(old) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(deployments)")}
        self.assertTrue({"triggered_by", "analysis_mode", "change_reasons"} <= columns)


class PushRedeployEndpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["GITHUB_WEBHOOK_SECRET"] = SECRET
        os.environ["INFRAMORPH_FAKE_DELAY"] = "0"
        self.db = Path(self.tmp.name) / "cp.db"
        self.app = create_app(db_path=self.db)
        self.client = TestClient(self.app)
        self.project = self.client.post(
            "/api/projects", json={"repo_url": REPO, "branch": "main", "targets": ["local", "aws"]}
        ).json()["project_id"]
        self.store = self.app.state.store

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()
        os.environ.pop("GITHUB_WEBHOOK_SECRET", None)
        os.environ.pop("INFRAMORPH_FAKE_DELAY", None)

    def send(self, files, delivery, forced=False):
        body = json.dumps({
            "ref": "refs/heads/main", "before": BEFORE, "after": AFTER, "forced": forced,
            "commits": [{"added": [], "modified": files, "removed": []}],
            "repository": {"html_url": REPO},
        }).encode()
        signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        return self.client.post("/api/webhooks/github", content=body, headers={
            "X-GitHub-Event": "push", "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": signature, "Content-Type": "application/json"})

    def only_redeploy(self, res):
        self.assertEqual(res.status_code, 202)
        redeploys = res.json()["redeploys"]
        self.assertEqual(len(redeploys), 1)
        return redeploys[0]

    def test_push_without_cache_starts_full_analysis_deployment(self):
        item = self.only_redeploy(self.send(["README.md"], "d1"))
        self.assertEqual(item["mode"], "full_analysis")
        self.assertEqual(item["state"], "started")
        deployment = self.client.get(f"/api/deployments/{item['deployment_id']}").json()
        self.assertEqual(deployment["commit_sha"], AFTER)
        self.assertEqual(deployment["triggered_by"], "push")
        self.assertEqual(deployment["status"], "LIVE")

    def test_cached_text_change_rebuilds_only(self):
        self.store.save_analysis(self.project, BEFORE, V1_REPO_MAP, V1_INTENT)
        item = self.only_redeploy(self.send(["README.md"], "d2"))
        self.assertEqual(item["mode"], "rebuild_only")

    def test_cached_dependency_change_reanalyzes(self):
        self.store.save_analysis(self.project, BEFORE, V1_REPO_MAP, V1_INTENT)
        item = self.only_redeploy(self.send(["package.json"], "d3"))
        self.assertEqual(item["mode"], "reanalyze")
        self.assertIn("의존성 파일 변경: package.json", item["reasons"])

    def test_push_during_running_deploy_is_queued(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE deployments SET status='DEPLOYING' WHERE project_id=?", (self.project,))
        item = self.only_redeploy(self.send(["README.md"], "d4"))
        self.assertEqual(item["state"], "queued")
        self.assertEqual(self.client.get(f"/api/deployments/{item['deployment_id']}").json()["status"], "CREATED")

    def test_branch_deletion_is_ignored(self):
        body = json.dumps({"ref": "refs/heads/main", "before": BEFORE, "after": "0" * 40, "deleted": True,
                           "commits": [], "repository": {"html_url": REPO}}).encode()
        signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        res = self.client.post("/api/webhooks/github", content=body, headers={
            "X-GitHub-Event": "push", "X-GitHub-Delivery": "del-x",
            "X-Hub-Signature-256": signature, "Content-Type": "application/json"})
        self.assertEqual(res.json()["ignored"], "branch deleted")
        self.assertEqual(len(self.client.get(f"/api/projects/{self.project}/deployments").json()), 1)

    def test_deployment_history_lists_push_deployments(self):
        self.send(["README.md"], "d5")
        history = self.client.get(f"/api/projects/{self.project}/deployments").json()
        self.assertEqual([d["triggered_by"] for d in history], ["push", "manual"])


class QueueDrainTest(unittest.TestCase):
    def test_push_arriving_mid_deploy_runs_after_current_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["INFRAMORPH_FAKE_DELAY"] = "0"
            calls = []

            def deployer_cmd(deployment_id, target):
                if not calls:  # 첫 배포가 도는 중에 push가 들어온 상황
                    store.create_push_deployment(project, AFTER, "rebuild_only", [])
                calls.append(deployment_id)
                return fake_deployer_cmd(deployment_id, target)

            app = create_app(db_path=Path(tmp) / "cp.db", deployer_cmd=deployer_cmd)
            store = app.state.store
            with TestClient(app) as client:
                project = client.post("/api/projects", json={"repo_url": REPO, "targets": ["local"]}).json()["project_id"]
                client.post(f"/api/projects/{project}/deploy")
                history = client.get(f"/api/projects/{project}/deployments").json()
            os.environ.pop("INFRAMORPH_FAKE_DELAY", None)
            store.close()
        self.assertEqual([d["status"] for d in history], ["LIVE", "LIVE"])
        self.assertEqual(len(set(calls)), 2)


if __name__ == "__main__":
    unittest.main()
