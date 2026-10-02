import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from tests.cp_isolation import NO_MODULES  # noqa: F401
from control_plane.db import Store
from control_plane.orchestrator import run_deployment
from control_plane.verify import check_url


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/health" else 500)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def log_message(self, *args):
        pass


class CheckUrlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_bare_address_gets_health_path_and_succeeds(self):
        result = check_url(self.base, "/health")
        self.assertEqual((result["status"], result["code"], result["url"]), ("ok", 200, self.base + "/health"))

    def test_error_status_and_closed_port_fail(self):
        self.assertEqual(check_url(self.base + "/broken")["detail"], "HTTP 500")
        closed = check_url("http://127.0.0.1:9")  # discard 포트: 듣는 프로그램이 없다
        self.assertEqual(closed["status"], "fail")
        self.assertIn("연결 실패", closed["detail"])

    def test_fake_deployer_addresses_are_not_called(self):
        self.assertEqual(check_url("https://example.invalid")["status"], "skipped")


class VerifyAfterDeployTest(unittest.TestCase):
    def test_live_target_is_checked_and_recorded_on_timeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "cp.db")
            project = store.create_project("https://github.com/o/r", "main", ["local"])
            dep = store.begin_deploy(project["project_id"])
            line = json.dumps({"deployment_id": dep, "ts": "2026-10-02T00:00:00Z", "target": "local",
                               "step": "url", "status": "ok", "url": "http://127.0.0.1:9"})
            cmd = [sys.executable, "-c", f"print({line!r})"]
            check = {"status": "ok", "url": "http://127.0.0.1:9/health", "code": 200, "ms": 120, "checked_at": "t"}
            run_deployment(store, dep, {"local": cmd}, verify=lambda target, url: check)
            deployment = store.get_deployment(dep)
            last = store.list_events(dep)[-1]["event"]
            store.close()
        self.assertEqual(deployment["targets"]["local"]["verification"], check)
        self.assertEqual((last["step"], last["status"]), ("health", "ok"))
        self.assertEqual(last["detail"], "조종실 직접 확인: 200 · 0.12초")


if __name__ == "__main__":
    unittest.main()
