"""Live-backend wiring uses offline provider replies; no paid API or deployment."""
import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from analyzer.backend import BackendError, ReplayBackend
from analyzer.local_verify import child_environment
from control_plane.analysis import AnalysisFailed
from control_plane.app import create_app
from control_plane.b_bridge import DemoModules, GitHubModules
from control_plane.db import Store
from control_plane.environment import load_server_environment
from control_plane.local_deploy import load_context
from control_plane.orchestrator import module_environment
from control_plane.runtime import LocalRuntime, analysis_backend, analysis_session
from schemas import Intent, RepoMap

ROOT = Path(__file__).resolve().parents[1]
KEY = 'operator-only-api-canary'


class ProviderReply:
    name = 'openai'
    known_secrets = (KEY,)

    def __init__(self, case='v1', error=None):
        self.replay = ReplayBackend.from_file(ROOT / f'tests/fixtures/analyzer/{case}/replay.json')
        self.error = error
        self.closed = False
        self.loop = None

    async def respond(self, **request):
        self.loop = asyncio.get_running_loop()
        if self.error:
            raise BackendError(self.error)
        return await self.replay.respond(**request)

    async def close(self):
        if self.loop is not None:
            assert asyncio.get_running_loop() is self.loop
        self.closed = True


class OpenAIRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(folder).resolve()
        self.stack.enter_context(patch.dict(os.environ, {'OPENAI_API_KEY': KEY}))
        self.store = Store(self.root / 'cp.db')
        self.addCleanup(self.store.close)

    def test_api_backend_and_clients_survive_successive_requests_without_fixture_fallback(self):
        runtime = LocalRuntime(root=self.root / 'runtime', b_modules=DemoModules(), openai=True)
        runtime.aws_config = self.root / 'analysis-only-aws.json'
        for case in ('v1', 'v2'):
            provider = ProviderReply(case)
            project = self.store.create_project('https://github.com/Team-InfraMorph/demo-app', 'main', ['local', 'aws'])
            sha = next(v['commit_sha'] for v in runtime.demo_versions() if v['id'] == case)
            did = self.store.begin_deploy(project['project_id'], revision=sha)
            with patch('control_plane.runtime.OpenAIBackend', return_value=provider), \
                 patch.object(DemoModules, 'replay', side_effect=AssertionError('no fixture fallback')), \
                 patch('control_plane.runtime.CodexBackend', side_effect=AssertionError('no Codex fallback')):
                result = runtime.analyze(self.store, self.store.get_deployment(did))
            self.assertTrue(provider.closed)
            self.assertEqual(result['metrics']['backend'], 'openai')
            self.assertGreater(result['metrics']['api_calls'], 0)
            self.assertEqual(result['metrics']['model'], 'gpt-6-luna')
            context = load_context(runtime.context_file(did))
            self.assertEqual(context.analysis_backend, 'openai')
            self.assertIsNone(context.replay)
            self.assertEqual(set(result['plans']), {'local', 'aws'})
            self.assertIn('--openai', runtime.command(self.store, did, 'local'))
            self.assertNotIn('--openai', runtime.command(self.store, did, 'aws'))
            self.assertNotIn(KEY, runtime.context_file(did).read_text())

    def test_provider_error_is_reported_with_usage_and_closed_client_without_fallback(self):
        runtime = LocalRuntime(root=self.root / 'runtime', b_modules=DemoModules(), openai=True)
        project = self.store.create_project('https://github.com/Team-InfraMorph/demo-app', 'main', ['local'])
        did = self.store.begin_deploy(project['project_id'])
        provider = ProviderReply(error='api_insufficient_quota')
        with patch('control_plane.runtime.OpenAIBackend', return_value=provider), \
             patch('control_plane.runtime.CodexBackend', side_effect=AssertionError('no Codex fallback')):
            with self.assertRaises(AnalysisFailed) as raised:
                runtime.analyze(self.store, self.store.get_deployment(did))
        self.assertEqual(raised.exception.code, 'api_insufficient_quota')
        self.assertEqual(raised.exception.metrics['api_calls'], 1)
        self.assertFalse(raised.exception.metrics['usage_complete'])
        self.assertTrue(provider.closed)
        self.assertFalse(runtime.context_file(did).exists())

    def test_recovery_session_closes_client_on_cancel(self):
        provider = ProviderReply()
        async def cancelled():
            async with analysis_session('openai', 'gpt-6-luna', None):
                raise asyncio.CancelledError()
        with patch('control_plane.runtime.OpenAIBackend', return_value=provider):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(cancelled())
        self.assertTrue(provider.closed)

    def test_runtime_info_exposes_api_and_github_modes_without_credentials(self):
        runtime = LocalRuntime(root=self.root / 'runtime', b_modules=GitHubModules(), openai=True)
        app = create_app(db_path=self.root / 'info.db', runtime=runtime)
        self.addCleanup(app.state.store.close)
        with TestClient(app) as client:
            response = client.get('/api/runtime')
        self.assertEqual(response.json()['analysis_backend'], 'openai')
        self.assertEqual(response.json()['mapper_mode'], 'github')
        self.assertEqual(response.json()['model'], 'gpt-6-luna')
        self.assertEqual(response.json()['reasoning_effort'], 'low')
        self.assertEqual(len(response.json()['demo_versions']), 2)
        self.assertNotIn(KEY, response.text)

    def test_api_startup_rejects_missing_key_conflicting_backend_and_wrong_model(self):
        for options, expected in (({'codex': True}, 'conflicting_analysis_backends'),
                                  ({'replay': 'unused'}, 'conflicting_analysis_backends'),
                                  ({'model': 'gpt-6-astra'}, 'unsupported_api_model')):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, expected):
                LocalRuntime(root=self.root / 'runtime', b_modules=GitHubModules(), openai=True, **options)
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, 'missing_openai_api_key'):
            LocalRuntime(root=self.root / 'runtime', b_modules=GitHubModules(), openai=True)
        with self.assertRaisesRegex(ValueError, 'conflicting_analysis_backends'):
            analysis_backend('openai', 'gpt-6-luna', 'unused-replay')

    def test_key_reaches_only_explicit_trusted_recovery_worker(self):
        command = [sys.executable, '-m', 'control_plane.local_deploy', '--context', '/private/context',
                   '--database', '/private/db', '--openai']
        self.assertEqual(module_environment('local', command)['OPENAI_API_KEY'], KEY)
        for target, cmd in [('aws', command), ('local', command[:-1]),
                            ('local', ['other-python', *command[1:]]),
                            ('local', [sys.executable, '-m', 'repo_mapper']),
                            ('local', [sys.executable, '-m', 'planner']),
                            ('local', [sys.executable, '-m', 'builder']),
                            ('local', [sys.executable, '-m', 'control_plane.fake_deployer'])]:
            self.assertNotIn('OPENAI_API_KEY', module_environment(target, cmd))
        self.assertNotIn('OPENAI_API_KEY', child_environment())

    def test_github_bridge_forwards_selected_revision_and_uses_real_planner(self):
        modules = GitHubModules()
        mapping = RepoMap.model_validate_json((ROOT / 'tests/fixtures/analyzer/v2/repo_map.json').read_text())
        source = self.root / 'snapshot'
        source.mkdir()
        project = {'repo_url': 'https://github.com/Team-InfraMorph/demo-app', 'branch': 'main'}
        with patch('control_plane.b_bridge.call_json', return_value={'snapshot': 'snapshot', 'repo_map': mapping.model_dump(mode='json')}) as call:
            mapped = modules.map(project, {'commit_sha': mapping.commit}, self.root)
        args, kwargs = call.call_args
        self.assertEqual(args[0], [sys.executable, '-m', 'repo_mapper'])
        self.assertEqual(args[1]['source_revision'], mapping.commit)
        self.assertEqual(mapped.snapshot, source)
        intent = Intent.model_validate_json((ROOT / 'schemas/fixtures/v2/intent.json').read_text())
        intent.config['PORT'] = '3000'
        for target in ('local', 'aws'):
            plan = modules.plan(intent, target)
            self.assertEqual(plan.config['PORT'], '3000')
            self.assertEqual([s.name for s in plan.services], ['web', 'worker'])

    def test_dotenv_uses_server_root_and_preserves_existing_environment(self):
        server = self.root / 'server'
        source = self.root / 'source'
        server.mkdir()
        source.mkdir()
        (server / '.env').write_text('OPENAI_API_KEY="from-server"\nTEST_LITERAL=${HOME}\n')
        (source / '.env').write_text('OPENAI_API_KEY=untrusted-source\n')
        previous = Path.cwd()
        try:
            os.chdir(source)
            with patch.dict(os.environ, {'OPENAI_API_KEY': 'from-process'}, clear=True), \
                 patch('control_plane.environment.ROOT', server):
                load_server_environment()
                self.assertEqual(os.environ['OPENAI_API_KEY'], 'from-process')
                self.assertEqual(os.environ['TEST_LITERAL'], '${HOME}')
            with patch.dict(os.environ, {}, clear=True), patch('control_plane.environment.ROOT', server):
                load_server_environment()
                self.assertEqual(os.environ['OPENAI_API_KEY'], 'from-server')
            with patch.dict(os.environ, {}, clear=True):
                load_server_environment(server / '.env')
                self.assertEqual(os.environ['OPENAI_API_KEY'], 'from-server')
        finally:
            os.chdir(previous)


if __name__ == '__main__':
    unittest.main()
