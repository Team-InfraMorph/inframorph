import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from analyzer.e_runtime import EConnector, classify_e_failure
from analyzer import ReplayBackend, Reply
from analyzer.feedback import LocalFailure, PatchReference
from analyzer.recovery import fingerprint, recover_local
from analyzer.retry_store import RetryStore
from code_patch import patch_snapshot
from schemas import BuildArtifact, Intent, Plan, RepoMap


ROOT = Path(__file__).resolve().parents[1]


class EConnectorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.mapping = RepoMap.model_validate_json((ROOT / "tests/fixtures/analyzer/v1/repo_map.json").read_text())
        self.intent = Intent.model_validate_json((ROOT / "schemas/fixtures/v1/intent.json").read_text())
        self.plan = Plan.model_validate_json((ROOT / "schemas/fixtures/v1/plan.local.json").read_text())
        self.artifact = BuildArtifact(source_revision=self.mapping.commit, target="local",
                                      image=self.plan.image_tag, platform="linux/amd64")
        self.connector = EConnector(snapshot=ROOT / "tests/fixtures/analyzer/v1/snapshot", repo_map=self.mapping,
            state_root=Path(temporary.name).resolve(), runtime_name="c-e-isolated", deployment_id="d-1",
            make_plan=AsyncMock(return_value=self.plan))

    def test_only_proven_application_failures_are_retryable(self):
        for code in ("smoke_http_failed", "health_or_tunnel_timeout", "command_failed",
                     "initial_deployment_cleanup_failed", "unknown"):
            self.assertFalse(classify_e_failure(code).retryable)
        for code in ("health_payload_failed", "note_persistence_failed", "image_persistence_failed"):
            self.assertTrue(classify_e_failure(code).retryable)

    async def test_runtime_namespace_and_publish_are_operator_owned(self):
        with patch("analyzer.e_runtime.run_worker", AsyncMock(return_value={"ok": True,
                   "deployment": {"url": "http://127.0.0.1:12345", "image_id": "test-image"}})) as worker:
            checked = await self.connector.check_local(self.artifact, self.plan)
        self.assertTrue(checked.ok)
        payload = worker.call_args.args[0]
        self.assertEqual(payload["plan"]["app"], "c-e-isolated")
        self.assertEqual(self.plan.app, "demo-app")
        self.assertFalse(payload["publish"])
        self.assertEqual(payload["artifact"]["source_revision"], self.mapping.commit)

    async def test_real_gate_failure_cannot_become_an_approval_receipt(self):
        with patch("analyzer.e_runtime.run_worker", AsyncMock(side_effect=ValueError("intent_source_mismatch"))):
            with self.assertRaises(ValueError):
                await self.connector.validate_intent(self.intent, fingerprint(self.intent))

    async def test_cancellation_cleans_only_owned_namespace(self):
        worker = AsyncMock(side_effect=asyncio.CancelledError())
        self.connector.cleanup = AsyncMock(return_value={"ok": True})
        with patch("analyzer.e_runtime.run_worker", worker), self.assertRaises(asyncio.CancelledError):
            await self.connector.check_local(self.artifact, self.plan)
        self.connector.cleanup.assert_awaited_once()

    async def test_adapter_code_cannot_authorize_retry_from_logs(self):
        with patch("analyzer.e_runtime.run_worker", AsyncMock(return_value={"ok": False, "code": "smoke_http_failed"})):
            result = await self.connector.check_local(self.artifact, self.plan)
        self.assertFalse(result.ok)
        self.assertFalse(result.failure.retryable)
        self.assertEqual(result.failure.log, "")

    async def test_real_e_gate_when_team_checkout_is_available(self):
        if not (ROOT / "policy_gate/gate.py").is_file():
            self.skipTest("E modules are checked in the separately assembled team checkout")
        receipt = await self.connector.validate_intent(self.intent, fingerprint(self.intent))
        self.assertTrue(receipt.approved)
        bad = self.intent.model_copy(deep=True)
        bad.workloads[0].port = 9999
        with self.assertRaisesRegex(ValueError, "intent_source_mismatch"):
            await self.connector.validate_intent(bad, fingerprint(bad))

    async def test_actual_e_worker_rejects_plan_execution_options_before_patch(self):
        if not (ROOT / "policy_gate/gate.py").is_file():
            self.skipTest("requires E modules")
        from analyzer.e_runtime import run_worker
        plan = self.plan.model_copy(deep=True)
        plan.config["NODE_OPTIONS"] = "--require ./src/server.js"
        with self.assertRaisesRegex(ValueError, "plan_source_mismatch"):
            await run_worker(self.connector.payload("patch", bundle="unused",
                plan=plan.model_dump(mode="json")))

    async def test_injected_intent_stops_before_planner_patch_and_builder_with_actual_e(self):
        if not (ROOT / "policy_gate/gate.py").is_file():
            self.skipTest("E modules are checked in the separately assembled team checkout")
        work = self.connector.state.parent
        manifest = patch_snapshot(self.connector.snapshot, self.mapping, self.plan, work / "initial")
        bad = self.intent.model_copy(deep=True)
        bad.workloads[0].port = 9999
        reads = [{"type": "function_call", "name": "Read", "call_id": "read-" + str(i),
                  "arguments": json.dumps({"path": path, "start_line": 1, "line_count": 200})}
                 for i, path in enumerate(self.mapping.tree)]
        result = await recover_local(deployment_id="d-injected", repo_map=self.mapping,
            snapshot_dir=self.connector.snapshot, previous_intent=self.intent, previous_plan=self.plan,
            previous_patch=PatchReference.from_manifest(manifest),
            failure=LocalFailure(stage="smoke", code="image_readback_failed"),
            backend=ReplayBackend([Reply(output=reads), Reply(text=bad.model_dump_json())]),
            hooks=self.connector.hooks(), store=RetryStore(work / "state/retry.sqlite"), output_dir=work / "retry")
        self.assertEqual(result.status, "failed")
        self.connector.make_plan.assert_not_awaited()
        self.assertFalse((work / "retry").exists())
        self.assertFalse(self.connector.state.exists())


if __name__ == "__main__":
    unittest.main()
