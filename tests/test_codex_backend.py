import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from analyzer.backend import BackendError
from analyzer.codex_backend import CodexBackend, _execute, _usage
from analyzer.config import Limits
from analyzer.local_verify import prepare
from analyzer.runner import AnalysisError, analyze
from control_plane.b_bridge import DemoModules
from control_plane.db import Store
from control_plane.local_deploy import load_context
from control_plane.runtime import LocalRuntime, analysis_backend


ROOT = Path(__file__).resolve().parents[1]
COMPLETE = json.dumps({"type": "turn.completed", "usage": {"input_tokens": 120, "output_tokens": 20}})


class CodexBackendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mapping, self.snapshot, _ = prepare("v1")
        self.source = ROOT / "tests/fixtures/analyzer/v1/snapshot"
        self.intent = (ROOT / "schemas/fixtures/v1/intent.json").read_text()

    def replies(self):
        reads = [{"name": "Read", "arguments": json.dumps({"path": path, "start_line": 1, "line_count": 200})}
                 for path in self.snapshot.files]
        return iter([{"tool_calls": reads, "text": ""}, {"tool_calls": [], "text": self.intent}])

    async def test_host_loop_uses_fresh_codex_replies_and_observed_evidence(self):
        replies = self.replies()
        prompts = []

        async def execute(argv, work, payload, timeout):
            if argv[1:3] == ["login", "status"]:
                return 0, "Logged in using ChatGPT"
            prompts.append(payload.decode())
            Path(argv[argv.index("--output-last-message") + 1]).write_text(json.dumps(next(replies)))
            self.assertIn("--output-schema", argv)
            self.assertIn("read-only", argv)
            self.assertIn('forced_login_method="chatgpt"', argv)
            self.assertIn('model_reasoning_effort="low"', argv)
            return 0, COMPLETE

        with patch("analyzer.codex_backend._execute", side_effect=execute), \
                patch("analyzer.codex_backend.shutil.which", return_value="codex"):
            result = await analyze(self.mapping, self.source, CodexBackend())
        self.assertEqual(result.intent.source_revision, self.mapping.commit)
        self.assertEqual(result.metrics.backend, "codex-cli")
        self.assertEqual(result.metrics.model, "gpt-6-luna")
        self.assertEqual(result.metrics.model_calls, 2)
        self.assertEqual(result.metrics.api_calls, 0)
        self.assertGreater(result.metrics.tool_calls, 0)
        self.assertEqual(result.metrics.input_tokens, 240)
        self.assertEqual(result.metrics.estimated_usd, 0)
        self.assertTrue(result.metrics.usage_complete)
        self.assertNotIn(self.intent, prompts[0])
        self.assertIn("UNTRUSTED", prompts[1])

    async def test_final_answer_without_source_observation_is_rejected(self):
        async def execute(argv, work, payload, timeout):
            if argv[1:3] == ["login", "status"]:
                return 0, "Logged in using ChatGPT"
            Path(argv[argv.index("--output-last-message") + 1]).write_text(
                json.dumps({"tool_calls": [], "text": self.intent}))
            return 0, COMPLETE
        with patch("analyzer.codex_backend._execute", side_effect=execute), \
                patch("analyzer.codex_backend.shutil.which", return_value="codex"), \
                self.assertRaises(AnalysisError) as raised:
            await analyze(self.mapping, self.source, CodexBackend())
        self.assertEqual(raised.exception.code, "invalid_intent")
        self.assertEqual(raised.exception.metrics.validation_retries, 1)

    async def test_api_key_authentication_fails_without_model_call_or_replay(self):
        with patch("analyzer.codex_backend._execute", return_value=(0, "Logged in using an API key")) as execute, \
                patch("analyzer.codex_backend.shutil.which", return_value="codex"), \
                self.assertRaises(AnalysisError) as raised:
            await analyze(self.mapping, self.source, CodexBackend())
        self.assertEqual(raised.exception.code, "chatgpt_login_required")
        self.assertEqual(execute.await_count, 1)
        self.assertFalse(raised.exception.metrics.usage_complete)

    async def test_timeout_terminates_child_and_output_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                await _execute([sys.executable, "-c", "import time; time.sleep(60)"], directory, b"", .1)
            self.assertLess(time.monotonic() - started, 3)
            with self.assertRaisesRegex(BackendError, "codex_log_size_limit"):
                await _execute([sys.executable, "-c", "print('x' * 2100000)"], directory, b"", 3)

    async def test_subprocess_does_not_inherit_api_keys(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"OPENAI_API_KEY": "canary", "CODEX_API_KEY": "canary"}):
            code, log = await _execute([sys.executable, "-c", "import os; print('OPENAI_API_KEY' in os.environ or 'CODEX_API_KEY' in os.environ)"], directory, b"", 3)
        self.assertEqual(code, 0)
        self.assertEqual(log.strip(), "False")

    def test_native_tool_events_and_missing_usage_fail_closed(self):
        for kind in ("command_execution", "mcp_tool_call", "web_search", "file_change"):
            with self.subTest(kind=kind), self.assertRaisesRegex(BackendError, "unexpected_codex_tool_use"):
                _usage(json.dumps({"type": "item.completed", "item": {"type": kind}}) + "\n" + COMPLETE)
        with self.assertRaisesRegex(BackendError, "codex_usage_missing"):
            _usage('{"type":"turn.completed"}')
        diagnostic = json.dumps({"type": "item.completed", "item": {"type": "error"}})
        self.assertEqual(_usage(diagnostic + "\n" + COMPLETE)["input_tokens"], 120)
        with self.assertRaisesRegex(BackendError, "codex_usage_missing"):
            _usage(diagnostic)

    def test_codex_backend_selection_never_reads_replay(self):
        with self.assertRaisesRegex(ValueError, "conflicting_analysis_backends"):
            analysis_backend("codex-cli", "gpt-6-astra", "private-replay.json")
        with self.assertRaisesRegex(ValueError, "unsupported_local_model"):
            CodexBackend("untrusted-model")


class CodexRuntimeTests(unittest.TestCase):
    def test_selected_backend_and_model_are_bound_to_recovery_context(self):
        class Backend:
            name = "codex-cli"
            model = "gpt-6-luna"
            known_secrets = ()

            def __init__(self, model):
                from analyzer.backend import ReplayBackend
                self.replay = ReplayBackend.from_file(ROOT / "tests/fixtures/analyzer/v1/replay.json")

            async def respond(self, **request):
                return await self.replay.respond(**request)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            store = Store(root / "control-plane.db")
            try:
                project = store.create_project("https://github.com/Team-InfraMorph/demo-app", "main", ["local"])
                did = store.begin_deploy(project["project_id"])
                runtime = LocalRuntime(root=root / "runtime", b_modules=DemoModules(), codex=True, model="gpt-6-luna")
                with patch("control_plane.runtime.CodexBackend", Backend), \
                        patch.object(DemoModules, "replay", side_effect=AssertionError("No fixture fallback")):
                    result = runtime.analyze(store, store.get_deployment(did))
                self.assertEqual(result["metrics"]["model"], "gpt-6-luna")
                context = load_context(runtime.context_file(did))
                self.assertEqual(context.analysis_backend, "codex-cli")
                self.assertEqual(context.analysis_model, "gpt-6-luna")
                self.assertIsNone(context.replay)
                with patch("control_plane.runtime.CodexBackend", Backend):
                    backend = analysis_backend(context.analysis_backend, context.analysis_model, context.replay)
                self.assertEqual(backend.name, "codex-cli")
            finally:
                store._conn.close()
