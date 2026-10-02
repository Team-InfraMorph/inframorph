import json
import sys
import tempfile
import unittest
from pathlib import Path

from control_plane.db import Store
from control_plane.orchestrator import mask_secrets, run_deployment


def script(lines, exit_code=0):
    body = "".join(f"print({json.dumps(line)}, flush=True)\n" for line in lines)
    return [sys.executable, "-c", body + f"raise SystemExit({exit_code})"]


def event(deployment_id, **extra):
    data = {"deployment_id": deployment_id, "ts": "2026-10-01T00:00:00Z",
            "target": "local", "step": "build", "status": "ok"}
    data.update(extra)
    return json.dumps(data)


class OrchestratorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "cp.db")
        project = self.store.create_project("https://github.com/o/r", "main", ["local"])
        self.dep = project["deployment_id"]
        self.store.begin_deploy(project["project_id"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_invalid_lines_are_dropped_and_valid_ones_stored(self):
        lines = [
            "not json",
            event("d-someone-else"),
            event(self.dep, status="done"),
            event(self.dep, extra_field=1),
            event(self.dep, target="aws"),
            event(self.dep),
        ]
        result = run_deployment(self.store, self.dep, {"local": script(lines)})["local"]
        self.assertEqual(result.accepted, 1)
        self.assertEqual(result.rejected, 5)
        self.assertEqual(len(self.store.list_events(self.dep)), 1)
        self.assertEqual(self.store.get_deployment(self.dep)["status"], "LIVE")

    def test_fail_event_marks_failed_even_with_zero_exit(self):
        run_deployment(self.store, self.dep, {"local": script([event(self.dep, step="health", status="fail")])})
        self.assertEqual(self.store.get_deployment(self.dep)["status"], "FAILED")

    def test_missing_executable_marks_failed(self):
        run_deployment(self.store, self.dep, {"local": ["/nonexistent/deployer"]})
        self.assertEqual(self.store.get_deployment(self.dep)["status"], "FAILED")

    def test_secrets_in_detail_are_masked_before_storage(self):
        detail = "push with AKIAABCDEFGHIJKLMNOP and token=ghp_" + "a" * 36
        run_deployment(self.store, self.dep, {"local": script([event(self.dep, detail=detail)])})
        stored = self.store.list_events(self.dep)[0]["event"]["detail"]
        self.assertNotIn("AKIAABCDEFGHIJKLMNOP", stored)
        self.assertNotIn("ghp_", stored)

    def test_targets_run_in_parallel_and_any_failure_fails_whole(self):
        project = self.store.create_project("https://github.com/o/r2", "main", ["local", "aws"])
        dep = project["deployment_id"]
        self.store.begin_deploy(project["project_id"])
        ok = event(dep, step="url", url="http://localhost:3000")
        bad = event(dep, target="aws", step="health", status="fail")
        run_deployment(self.store, dep, {"local": script([ok]), "aws": script([bad])})
        deployment = self.store.get_deployment(dep)
        self.assertEqual(deployment["status"], "FAILED")
        self.assertEqual(deployment["targets"], {
            "aws": {"status": "FAILED", "url": None, "verification": None},
            "local": {"status": "LIVE", "url": "http://localhost:3000", "verification": None},
        })

    def test_mask_secrets_keeps_ordinary_text(self):
        self.assertEqual(mask_secrets("ECR push 3 layers"), "ECR push 3 layers")


if __name__ == "__main__":
    unittest.main()
