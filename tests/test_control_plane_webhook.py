import hashlib
import hmac
import json
import os
import subprocess
import tempfile
import unittest
import warnings
from pathlib import Path
from urllib.parse import urlencode

warnings.filterwarnings("ignore", category=DeprecationWarning)

from fastapi.testclient import TestClient  # noqa: E402

from control_plane.app import create_app  # noqa: E402
from control_plane.fake_push import build_payload, from_git  # noqa: E402
from control_plane.webhook import parse_push_details, verify_signature  # noqa: E402

SECRET = "test-secret"
REPO = "https://github.com/Team-InfraMorph/demo-app"


def push_body(ref="refs/heads/main", after="a" * 40, html_url=REPO, before="b" * 40, commits=None, forced=False):
    return json.dumps({
        "ref": ref,
        "before": before,
        "after": after,
        "forced": forced,
        "commits": commits or [],
        "repository": {"html_url": html_url},
    }).encode()


def sign(body, secret=SECRET):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class VerifySignatureTest(unittest.TestCase):
    def test_accepts_valid_and_rejects_others(self):
        body = b"{}"
        self.assertTrue(verify_signature(SECRET, body, sign(body)))
        self.assertFalse(verify_signature(SECRET, body, sign(body, "other")))
        self.assertFalse(verify_signature(SECRET, body, None))
        self.assertFalse(verify_signature(SECRET, body, "sha1=" + "0" * 40))
        self.assertFalse(verify_signature("", body, sign(body, "")))


class WebhookEndpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["GITHUB_WEBHOOK_SECRET"] = SECRET
        os.environ["INFRAMORPH_FAKE_DELAY"] = "0"
        self.client = TestClient(create_app(db_path=Path(self.tmp.name) / "cp.db"))
        res = self.client.post("/api/projects", json={"repo_url": REPO + ".git", "branch": "main", "targets": ["local"]})
        self.project_id = res.json()["project_id"]

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()
        os.environ.pop("GITHUB_WEBHOOK_SECRET", None)
        os.environ.pop("INFRAMORPH_FAKE_DELAY", None)

    def post(self, body, delivery="del-1", event="push", signature=None):
        headers = {
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": signature if signature is not None else sign(body),
            "Content-Type": "application/json",
        }
        return self.client.post("/api/webhooks/github", content=body, headers=headers)

    def test_valid_push_matches_project(self):
        body = push_body(commits=[{"modified": ["package.json"], "added": [], "removed": []}], forced=True)
        res = self.post(body)
        self.assertEqual(res.status_code, 202)
        self.assertEqual(res.json()["projects"], [self.project_id])
        self.assertEqual(res.json()["commit"], "a" * 40)
        self.assertEqual(res.json()["before"], "b" * 40)
        self.assertEqual(res.json()["changed_files"], ["package.json"])
        self.assertTrue(res.json()["forced"])

    def test_bad_signature_is_rejected(self):
        self.assertEqual(self.post(push_body(), signature=sign(b"other")).status_code, 401)

    def test_missing_secret_fails_closed(self):
        os.environ.pop("GITHUB_WEBHOOK_SECRET")
        self.assertEqual(self.post(push_body()).status_code, 503)

    def test_duplicate_delivery_is_ignored(self):
        self.assertEqual(self.post(push_body(), delivery="same").status_code, 202)
        second = self.post(push_body(), delivery="same")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["ignored"], "duplicate")

    def test_other_branch_and_unknown_repo_are_ignored(self):
        self.assertEqual(self.post(push_body(ref="refs/heads/dev"), delivery="d2").json()["ignored"], "no matching project")
        other = push_body(html_url="https://github.com/someone/else")
        self.assertEqual(self.post(other, delivery="d3").json()["ignored"], "no matching project")

    def test_ping_and_non_push_events(self):
        self.assertEqual(self.post(b"{}", event="ping", delivery="p1").json()["ok"], True)
        self.assertEqual(self.post(b"{}", event="issues", delivery="p2").json()["ignored"], "event")

    def test_form_encoded_payload_is_accepted(self):
        body = urlencode({"payload": push_body().decode()}).encode()
        res = self.client.post("/api/webhooks/github", content=body, headers={
            "X-GitHub-Event": "push", "X-GitHub-Delivery": "f1", "X-Hub-Signature-256": sign(body),
            "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(res.status_code, 202)

    def test_tag_push_is_ignored(self):
        self.assertEqual(self.post(push_body(ref="refs/tags/v1"), delivery="t1").json()["ignored"], "tag push")

    def test_fake_push_payload_matches_parser(self):
        body = json.dumps(build_payload(REPO, "main", "b" * 40, "a" * 40,
                                        {"added": [], "modified": ["README.md"], "removed": []})).encode()
        self.assertEqual(self.post(body, delivery="fp1").json()["changed_files"], ["README.md"])

    def test_tunnel_requests_reach_only_the_webhook(self):
        tunnel = {"Cf-Ray": "8f00-ICN"}
        self.assertEqual(self.client.get("/api/projects", headers=tunnel).status_code, 403)
        self.assertEqual(self.client.post(f"/api/projects/{self.project_id}/deploy", headers=tunnel).status_code, 403)
        self.assertEqual(self.client.get("/", headers=tunnel).status_code, 403)
        body = push_body()
        res = self.client.post("/api/webhooks/github", content=body, headers={
            **tunnel, "X-GitHub-Event": "push", "X-GitHub-Delivery": "cf1", "X-Hub-Signature-256": sign(body)})
        self.assertEqual(res.status_code, 202)
        self.assertEqual(self.client.get("/api/projects").status_code, 200)

    def test_malformed_payload_is_400(self):
        self.assertEqual(self.post(b"not json", delivery="m1").status_code, 400)
        self.assertEqual(self.post(json.dumps({"ref": 1}).encode(), delivery="m2").status_code, 400)


class FakePushFromGitTest(unittest.TestCase):
    def test_reads_last_commit_including_rename(self):
        with tempfile.TemporaryDirectory() as repo:
            def git(*args):
                subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t", *args],
                               check=True, capture_output=True)
            for name in ("a.txt", "b.txt"):
                Path(repo, name).write_text(name * 20)
            git("init", "-q")
            git("add", ".")
            git("commit", "-qm", "one")
            git("mv", "a.txt", "c.txt")
            Path(repo, "b.txt").write_text("changed")
            git("commit", "-qam", "two")
            before, after, files = from_git(repo)
        self.assertNotEqual(before, after)
        self.assertEqual(files, {"added": ["c.txt"], "modified": ["b.txt"], "removed": ["a.txt"]})
        push = parse_push_details(build_payload(REPO, "main", before, after, files))
        self.assertEqual(set(push.changed_files), {"a.txt", "b.txt", "c.txt"})


if __name__ == "__main__":
    unittest.main()
