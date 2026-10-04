import os
import json
import sqlite3
import tempfile
import unittest
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=DeprecationWarning)

from fastapi.testclient import TestClient  # noqa: E402

from tests.cp_isolation import NO_MODULES  # noqa: E402,F401
from control_plane.app import create_app  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "schemas" / "fixtures" / "events"
REPO = "https://github.com/Team-InfraMorph/demo-app"


class ControlPlaneApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "cp.db"
        os.environ["INFRAMORPH_FAKE_DELAY"] = "0"
        self.client = TestClient(create_app(db_path=self.db))

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()
        os.environ.pop("INFRAMORPH_FAKE_DELAY", None)
        os.environ.pop("INFRAMORPH_FAKE_FIXTURE", None)

    def create(self, **overrides):
        body = {"repo_url": REPO, "branch": "main", "targets": ["local", "aws"]}
        body.update(overrides)
        return self.client.post("/api/projects", json=body)

    def test_create_project_returns_ids_and_created_status(self):
        res = self.create()
        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertTrue(data["project_id"].startswith("p-"))
        self.assertTrue(data["deployment_id"].startswith("d-"))
        self.assertEqual(data["status"], "CREATED")

    def test_project_survives_restart(self):
        data = self.create().json()
        restarted = TestClient(create_app(db_path=self.db))
        res = restarted.get(f"/api/projects/{data['project_id']}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["repo_url"], REPO)
        self.assertEqual(res.json()["targets"], ["local", "aws"])
        restarted.close()

    def test_saved_patch_is_visible_without_execution_runtime(self):
        data = self.create().json()
        patch = {"status": "changed", "files": [{"path": "app.js", "action": "modify", "diff": "-old\n+new"}], "verified": True}
        with sqlite3.connect(self.db) as db:
            db.execute("INSERT INTO deployment_patch_reviews VALUES (?, ?, ?, ?, ?)",
                       (data["deployment_id"], "local", "initial", "test-fingerprint", json.dumps(patch)))
        restarted = TestClient(create_app(db_path=self.db))
        try:
            response = restarted.get(f"/api/deployments/{data['deployment_id']}/patch")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["local"]["files"], patch["files"])
        finally:
            restarted.close()

    def test_invalid_inputs_are_rejected(self):
        cases = [
            {"repo_url": "ftp://example.com/x"},
            {"repo_url": "https://evil.example/owner/repo"},
            {"targets": []},
            {"targets": ["azure"]},
            {"targets": ["aws", "aws"]},
            {"branch": "../main"},
            {"branch": "feature..x"},
            {"unexpected": "field"},
        ]
        for case in cases:
            with self.subTest(case=case):
                self.assertEqual(self.create(**case).status_code, 422)

    def test_projects_are_listed_newest_first(self):
        first = self.create().json()["project_id"]
        second = self.create().json()["project_id"]
        self.assertEqual([p["project_id"] for p in self.client.get("/api/projects").json()], [second, first])

    def test_unknown_project_is_404(self):
        self.assertEqual(self.client.get("/api/projects/p-missing").status_code, 404)

    def test_fake_deploy_streams_events_and_goes_live(self):
        project = self.create().json()
        res = self.client.post(f"/api/projects/{project['project_id']}/deploy")
        self.assertEqual(res.status_code, 202)
        deployment_id = res.json()["deployment_id"]
        self.assertEqual(deployment_id, project["deployment_id"])

        deployment = self.client.get(f"/api/deployments/{deployment_id}").json()
        self.assertEqual(deployment["status"], "LIVE")
        local = deployment["targets"]["local"]
        self.assertEqual((local["status"], local["url"]), ("LIVE", "http://local.invalid:3000"))
        self.assertEqual(local["verification"]["status"], "skipped")  # 가짜 배포기 주소는 직접 확인하지 않는다
        self.assertEqual(deployment["targets"]["aws"]["status"], "LIVE")

        with self.client.stream("GET", f"/api/deployments/{deployment_id}/events") as stream:
            body = "".join(stream.iter_text())
        # local + aws 각각: 분석 완료 1줄 + 코드 수정(시작·완료) 2줄 + 배포기 이벤트
        expected = 2 * (3 + len((FIXTURES / "happy_path.jsonl").read_text().splitlines()))
        self.assertEqual(body.count("event: deploy"), expected)
        self.assertIn("event: end", body)
        self.assertIn(deployment_id, body)

    def test_reverify_endpoint_refreshes_direct_checks(self):
        project = self.create().json()
        deployment_id = self.client.post(f"/api/projects/{project['project_id']}/deploy").json()["deployment_id"]
        targets = self.client.post(f"/api/deployments/{deployment_id}/verify").json()
        self.assertEqual({t: v["verification"]["status"] for t, v in targets.items()},
                         {"local": "skipped", "aws": "skipped"})
        self.assertEqual(self.client.post("/api/deployments/d-missing/verify").status_code, 404)

    def test_event_stream_resumes_after_last_event_id(self):
        project = self.create().json()
        deployment_id = self.client.post(f"/api/projects/{project['project_id']}/deploy").json()["deployment_id"]
        with self.client.stream(
            "GET", f"/api/deployments/{deployment_id}/events", headers={"Last-Event-ID": "3"}
        ) as stream:
            body = "".join(stream.iter_text())
        self.assertNotIn("id: 3\n", body)
        self.assertIn("id: 4\n", body)

    def test_health_failure_with_rollback_marks_rolled_back(self):
        os.environ["INFRAMORPH_FAKE_FIXTURE"] = str(FIXTURES / "rollback.jsonl")
        project = self.create(targets=["local"]).json()
        deployment_id = self.client.post(f"/api/projects/{project['project_id']}/deploy").json()["deployment_id"]
        status = self.client.get(f"/api/deployments/{deployment_id}").json()["status"]
        self.assertEqual(status, "ROLLED_BACK")

    def test_one_step_deploy_reuses_project_and_picks_targets_per_deploy(self):
        # 한 화면 배포: 같은 레포·브랜치는 같은 프로젝트(같은 앱). 대상은 배포마다 고른다.
        os.environ["INFRAMORPH_FAKE_FIXTURE"] = str(FIXTURES / "happy_path.jsonl")
        body = {"repo_url": REPO, "branch": "main", "targets": ["local", "aws"]}
        first = self.client.post("/api/deploy", json=body)
        self.assertEqual(first.status_code, 202, first.text)
        second = self.client.post("/api/deploy", json=body | {"targets": ["local"]})
        self.assertEqual(second.json()["project_id"], first.json()["project_id"])
        self.assertEqual(len(self.client.get("/api/projects").json()), 1)
        deployment = self.client.get(f"/api/deployments/{second.json()['deployment_id']}").json()
        self.assertEqual(sorted(deployment["targets"]), ["local"])
        # 행선지를 줄였다고 '대상 제거' 승인을 받지 않는다(대상마다 그 대상의 직전 정상 구조와 비교).
        self.assertEqual(deployment["status"], "LIVE")
        self.assertEqual(self.client.get(f"/api/projects/{first.json()['project_id']}").json()["targets"], ["local"])

    def test_failed_local_test_blocks_other_targets(self):
        # Local 테스트가 실패(롤백)하면 AWS는 실행하지 않고 '배포하지 않음'으로 남긴다.
        os.environ["INFRAMORPH_FAKE_FIXTURE"] = str(FIXTURES / "rollback.jsonl")
        project = self.create().json()
        deployment_id = self.client.post(f"/api/projects/{project['project_id']}/deploy").json()["deployment_id"]
        deployment = self.client.get(f"/api/deployments/{deployment_id}").json()
        self.assertEqual(deployment["status"], "FAILED")
        self.assertEqual(deployment["targets"]["local"]["status"], "ROLLED_BACK")
        self.assertEqual(deployment["targets"]["aws"]["status"], "FAILED")
        events = sqlite3.connect(self.db).execute("SELECT payload FROM events WHERE deployment_id=?", (deployment_id,))
        aws = [e for (e,) in events if '"target":"aws"' in e.replace(" ", "")]
        self.assertIn("Local 테스트를 통과하지 못해", aws[-1])
        self.assertFalse(any('"step":"url"' in e.replace(" ", "") for e in aws))  # AWS 배포기는 실행되지 않음

    def test_aws_only_rollback_keeps_local_live(self):
        os.environ["INFRAMORPH_FAKE_FIXTURE_AWS"] = str(FIXTURES / "rollback.jsonl")
        try:
            project = self.create().json()
            deployment_id = self.client.post(f"/api/projects/{project['project_id']}/deploy").json()["deployment_id"]
        finally:
            os.environ.pop("INFRAMORPH_FAKE_FIXTURE_AWS", None)
        deployment = self.client.get(f"/api/deployments/{deployment_id}").json()
        self.assertEqual(deployment["status"], "ROLLED_BACK")
        self.assertEqual(deployment["targets"]["aws"]["status"], "ROLLED_BACK")
        self.assertEqual(deployment["targets"]["local"]["status"], "LIVE")

    def test_nonzero_exit_marks_failed(self):
        os.environ["INFRAMORPH_FAKE_FIXTURE"] = str(FIXTURES / "happy_path.jsonl")
        os.environ["INFRAMORPH_FAKE_EXIT_CODE"] = "1"
        try:
            project = self.create().json()
            deployment_id = self.client.post(f"/api/projects/{project['project_id']}/deploy").json()["deployment_id"]
            status = self.client.get(f"/api/deployments/{deployment_id}").json()["status"]
        finally:
            os.environ.pop("INFRAMORPH_FAKE_EXIT_CODE", None)
        self.assertEqual(status, "FAILED")

    def test_second_deploy_while_running_is_conflict(self):
        project = self.create().json()
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE deployments SET status='DEPLOYING' WHERE id=?", (project["deployment_id"],))
        res = self.client.post(f"/api/projects/{project['project_id']}/deploy")
        self.assertEqual(res.status_code, 409)

    def test_restart_marks_interrupted_deployments_failed(self):
        project = self.create().json()
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE deployments SET status='DEPLOYING' WHERE id=?", (project["deployment_id"],))
        restarted = TestClient(create_app(db_path=self.db))
        status = restarted.get(f"/api/deployments/{project['deployment_id']}").json()["status"]
        self.assertEqual(status, "FAILED")
        restarted.close()


if __name__ == "__main__":
    unittest.main()
