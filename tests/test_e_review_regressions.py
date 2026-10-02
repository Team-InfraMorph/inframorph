"""Regression coverage for PR29's integration review findings."""
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from control_plane.orchestrator import module_environment
from adapters.local.runtime import deploy, rollback
from builder.runtime import RuntimeFailure
from tests import test_e_recovery


class AdapterEnvironmentTests(unittest.TestCase):
    def test_real_aws_cli_parser_receives_operator_configuration(self):
        values = {
            'INFRAMORPH_FOUNDATION_OUTPUTS': '/unused/foundation.json',
            'INFRAMORPH_AWS_ACCOUNT_ID': '123456789012',
            'INFRAMORPH_TF_STATE_BUCKET': 'test-state',
            'AWS_PROFILE': 'test-profile', 'AWS_REGION': 'ap-northeast-2',
            'AWS_SECRET_ACCESS_KEY': 'dummy', 'OPENAI_API_KEY': 'must-not-forward',
        }
        with patch.dict(os.environ, values):
            for action in ('deploy', 'rollback'):
                env = module_environment('aws', [sys.executable, '-m', 'adapters.aws', action])
                self.assertNotIn('OPENAI_API_KEY', env)
                code = """
import os
from adapters.aws.cli import build_parser
args = build_parser().parse_args(ACTION + ['--plan', '/unused/plan', '--artifact', '/unused/artifact', '--state-dir', '/unused/state', '--deployment-id', 'test', '--execute'])
assert str(args.foundation) == '/unused/foundation.json'
assert args.account_id == '123456789012'
assert args.state_bucket == 'test-state'
assert os.environ['AWS_PROFILE'] == 'test-profile'
assert 'OPENAI_API_KEY' not in os.environ
""".replace('ACTION', repr([action]))
                result = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            for target, module in [('aws', 'builder'), ('local', 'adapters.local'), ('local', 'adapters.aws'), ('aws', 'analyzer.e_worker')]:
                env = module_environment(target, [sys.executable, '-m', module, 'deploy'])
                self.assertNotIn('AWS_SECRET_ACCESS_KEY', env)
                self.assertNotIn('INFRAMORPH_FOUNDATION_OUTPUTS', env)


class PublicRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_e_recovery.RecoveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.url = 'https://current-review.trycloudflare.com'

    def execute(self, args, **kwargs):
        if 'logs' in args:
            return self.url
        return self.fixture.execute(args, **kwargs)

    def deploy(self):
        f = self.fixture
        return deploy(f.plan, f.artifact, f.state, publish=True, execute=self.execute, sink=f.events.append)

    def test_public_rollback_rediscovery_checks_both_records(self):
        f = self.fixture
        with patch('adapters.local.runtime.request', return_value=b'{}'), patch('adapters.local.runtime.smoke', return_value=f.record) as smoke:
            self.deploy()
            self.deploy()
            self.url = 'https://restored-review.trycloudflare.com'
            smoke.reset_mock()
            result = rollback(f.state, execute=self.execute, sink=f.events.append)
        self.assertEqual(result['public_url'], self.url)
        self.assertEqual(f.events[-1].url, self.url)
        self.assertEqual(sum(c.args[0] == self.url for c in smoke.call_args_list), 2)

    def test_failed_upgrade_revalidates_previous_public_url(self):
        f = self.fixture
        with patch('adapters.local.runtime.request', return_value=b'{}'), patch('adapters.local.runtime.smoke', return_value=f.record):
            self.deploy()
        with patch('adapters.local.runtime.request', return_value=b'{}'), patch('adapters.local.runtime.wait_for', side_effect=lambda action, **kwargs: action()), patch('adapters.local.runtime.smoke', side_effect=[RuntimeFailure('upgrade_failed'), f.record, f.record]) as smoke:
            with self.assertRaisesRegex(RuntimeFailure, 'upgrade_failed'):
                self.deploy()
        self.assertEqual(smoke.call_args.args[0], self.url)
        self.assertEqual(f.events[-1].status, 'ok')
        self.assertEqual(f.events[-1].url, self.url)
        self.assertEqual(json.loads((f.state / 'current.json').read_text())['public_url'], self.url)

    def test_public_failure_never_promotes_rollback(self):
        f = self.fixture
        with patch('adapters.local.runtime.request', return_value=b'{}'), patch('adapters.local.runtime.smoke', return_value=f.record):
            self.deploy()
            self.deploy()
        before = (f.state / 'current.json').read_bytes()
        def smoke(url, **kwargs):
            if url.startswith('https:'):
                raise RuntimeFailure('public_unreachable')
            return f.record
        with patch('adapters.local.runtime.wait_for', side_effect=lambda action, **kwargs: action()), patch('adapters.local.runtime.smoke', side_effect=smoke):
            with self.assertRaisesRegex(RuntimeFailure, 'public_unreachable'):
                rollback(f.state, execute=self.execute, sink=f.events.append)
        self.assertEqual(f.events[-1].status, 'fail')
        self.assertEqual((f.state / 'current.json').read_bytes(), before)

    def test_failed_upgrade_and_public_restore_report_failure(self):
        f = self.fixture
        with patch('adapters.local.runtime.request', return_value=b'{}'), patch('adapters.local.runtime.smoke', return_value=f.record):
            self.deploy()
        before = (f.state / 'current.json').read_bytes()
        with patch('adapters.local.runtime.request', return_value=b'{}'), patch('adapters.local.runtime.wait_for', side_effect=lambda action, **kwargs: action()), patch('adapters.local.runtime.smoke', side_effect=[RuntimeFailure('upgrade_failed'), f.record, RuntimeFailure('public_unreachable')]):
            with self.assertRaisesRegex(RuntimeFailure, 'upgrade_failed'):
                self.deploy()
        self.assertEqual(f.events[-1].status, 'fail')
        self.assertEqual(f.events[-1].detail, 'restore_failed')
        self.assertEqual((f.state / 'current.json').read_bytes(), before)
