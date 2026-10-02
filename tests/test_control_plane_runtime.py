import copy
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from control_plane.app import create_app
from control_plane.b_bridge import BCommands, DemoModules, call_json
from control_plane.db import Store
from control_plane.local_deploy import load_context
from control_plane.orchestrator import run_deployment
from control_plane.runtime import LocalRuntime
from analyzer.source_policy import validate_demo_plan, SourcePolicyError
from schemas import Plan, RepoMap


ROOT = Path(__file__).resolve().parents[1]


def payload():
    fixture = ROOT / "schemas/fixtures/v1"
    return {"repo_map": json.loads((ROOT / "tests/fixtures/analyzer/v1/repo_map.json").read_text()),
            "intent": json.loads((fixture / "intent.json").read_text()),
            "plans": {"local": json.loads((fixture / "plan.local.json").read_text())},
            "metrics": {"backend": "replay", "model_calls": 2, "tool_calls": 4, "duration_ms": 20}}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root / "cp.db")
        self.project = self.store.create_project("https://github.com/Team-InfraMorph/demo-app", "main", ["local"])
        self.did = self.store.begin_deploy(self.project["project_id"])
        self.initial = payload()
        sha = self.initial["repo_map"]["commit"]
        self.store.set_commit(self.did, sha)
        self.store.save_analysis(self.project["project_id"], sha, self.initial["repo_map"], self.initial["intent"])
        self.store.save_plans(self.did, self.initial["plans"])
        self.store.save_initial_analysis(self.did, self.initial)

    def tearDown(self):
        self.store._conn.close()
        self.temp.cleanup()

    def recovered(self):
        corrected = copy.deepcopy(self.initial)
        corrected["intent"]["workloads"][0]["evidence"] = ["src/server.js:25"]
        return {"status": "recovered", "reason": "retry_recovered", "attempts": 1,
                "metrics": {"backend": "replay", "model_calls": 3, "tool_calls": 5}, "corrected": corrected}

    def test_recovery_commits_history_and_usage_once_even_after_restart(self):
        update = self.recovered()
        self.assertTrue(self.store.apply_recovery_analysis(self.did, update))
        reopened = Store(self.store.path)
        self.assertFalse(reopened.apply_recovery_analysis(self.did, update))
        result = reopened.get_deployment_analysis(self.did)
        self.assertEqual(result["metrics"]["model_calls"], 5)
        self.assertEqual(result["metrics"]["tool_calls"], 9)
        self.assertNotEqual(result["intent"], result["initial"]["intent"])
        self.assertEqual(reopened.get_analysis(self.project["project_id"], result["repo_map"]["commit"])["intent"], result["intent"])
        with self.assertRaises(ValueError):
            reopened.apply_recovery_analysis(self.did, update | {"reason": "another_result"})
        reopened._conn.close()

    def test_failed_retry_keeps_artifacts_but_counts_usage(self):
        self.store.apply_recovery_analysis(self.did, {"status": "failed", "reason": "reanalysis_failed", "attempts": 1,
            "metrics": {"backend": "replay", "model_calls": 1, "usage_complete": False}, "corrected": {"malicious": True}})
        result = self.store.get_deployment_analysis(self.did)
        self.assertEqual(result["intent"], self.initial["intent"])
        self.assertEqual(result["plans"], self.initial["plans"])
        self.assertEqual(result["metrics"]["model_calls"], 3)
        self.assertFalse(result["metrics"]["usage_complete"])

    def test_wrong_revision_cannot_promote_or_partially_update_usage(self):
        update = self.recovered()
        update["corrected"]["intent"]["source_revision"] = "a" * 40
        with self.assertRaises(ValueError):
            self.store.apply_recovery_analysis(self.did, update)
        self.assertNotIn("recovery", self.store.get_deployment_analysis(self.did))

    def test_same_commit_later_cache_does_not_change_earlier_history(self):
        self.store.apply_recovery_analysis(self.did, self.recovered())
        corrected = self.store.get_deployment_analysis(self.did)["intent"]
        self.store.set_status(self.did, "LIVE")
        second = self.store.begin_deploy(self.project["project_id"])
        self.store.set_commit(second, self.initial["repo_map"]["commit"])
        self.store.save_initial_analysis(second, self.initial)
        self.assertEqual(self.store.get_deployment_analysis(second)["intent"], self.initial["intent"])
        self.assertEqual(self.store.get_deployment_analysis(self.did)["intent"], corrected)

    def test_runtime_claim_survives_new_store(self):
        self.store.claim_runtime_run(self.did, self.initial["repo_map"]["commit"])
        reopened = Store(self.store.path)
        with self.assertRaises(ValueError):
            reopened.claim_runtime_run(self.did, self.initial["repo_map"]["commit"])
        reopened._conn.close()

    def test_demo_context_uses_real_c_analysis_and_is_bound_to_owned_paths(self):
        runtime = LocalRuntime(root=self.root / "runtime", b_modules=DemoModules())
        result = runtime.analyze(self.store, self.store.get_deployment(self.did))
        self.assertGreater(result["metrics"]["tool_calls"], 0)
        self.assertEqual(result["metrics"]["api_calls"], 0)
        path = runtime.context_file(self.did)
        context = load_context(path)
        self.assertTrue(context.demo)
        value = json.loads(path.read_text())
        value["snapshot"] = str(ROOT)
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            load_context(path)

    def test_non_demo_requires_explicit_b_and_response_backend(self):
        with self.assertRaises(ValueError):
            BCommands(mapper_command=None, planner_command=None)
        with self.assertRaises(ValueError):
            LocalRuntime(root=self.root / "runtime", b_modules=object())
        with self.assertRaises(ValueError):
            LocalRuntime(root=self.root / "runtime", b_modules=object(), replay="response.json", fault="first")

    def test_plan_cannot_inject_node_execution_options(self):
        plan = Plan.model_validate(self.initial["plans"]["local"])
        plan.config["NODE_OPTIONS"] = "--require ./src/server.js"
        with self.assertRaisesRegex(SourcePolicyError, "plan_source_mismatch"):
            validate_demo_plan(plan, RepoMap.model_validate(self.initial["repo_map"]))

    def test_command_bridge_does_not_interpret_source_text_as_shell(self):
        value = {"repo": "$(touch /tmp/inframorph-should-not-exist)"}
        result = call_json([sys.executable, "-c", "import json,sys; print(json.dumps(json.load(sys.stdin)))"], value)
        self.assertEqual(result, value)
        with self.assertRaises(ValueError):
            call_json("python -c something", value)

    def test_silent_child_obeys_timeout(self):
        start = time.monotonic()
        result = run_deployment(self.store, self.did, {"local": [sys.executable, "-c", "import time; time.sleep(30)"]}, timeout=0.15)
        self.assertLess(time.monotonic() - start, 3)
        self.assertEqual(result["local"].status.value, "FAILED")

    def test_api_reads_corrected_intent_and_original_intent(self):
        self.store.apply_recovery_analysis(self.did, self.recovered())
        app = create_app(db_path=self.store.path)
        with TestClient(app) as client:
            value = client.get(f"/api/deployments/{self.did}/analysis").json()
        self.assertEqual(value["metrics"]["model_calls"], 5)
        self.assertNotEqual(value["intent"], value["initial_intent"])


if __name__ == "__main__":
    unittest.main()
