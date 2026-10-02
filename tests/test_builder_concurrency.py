"""Exercise the shared image lock across real Local/AWS Builder processes."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from analyzer.local_verify import child_environment
from builder.runtime import RuntimeFailure, image_lock
from code_patch import patch_snapshot
from control_plane.local_deploy import public_failure_code
from policy_gate.gate import PolicyError


ROOT = Path(__file__).resolve().parents[1]
BUILD_CHILD = """
import json, sys, time
from pathlib import Path
from builder.runtime import build

root = Path(sys.argv[1])
target = sys.argv[2]
plan = json.loads((root / (target + '.json')).read_text())
cache = root / 'image.json'

def execute(args, **kwargs):
    if args[1] == 'info':
        return 'linux'
    if args[1:3] == ['image', 'ls']:
        return 'cached' if cache.exists() else ''
    if args[1:3] == ['image', 'inspect']:
        return cache.read_text()
    if args[1:3] == ['buildx', 'build']:
        (root / 'building').touch()
        deadline = time.monotonic() + 10
        while not (root / 'release').exists():
            if time.monotonic() > deadline:
                raise RuntimeError('test_build_not_released')
            time.sleep(0.01)
        labels = dict(args[i + 1].split('=', 1) for i, v in enumerate(args) if v == '--label')
        cache.write_text(json.dumps([{'Os': 'linux', 'Architecture': 'amd64',
                                     'Config': {'Labels': labels}}]))
        with (root / 'builds').open('a') as file:
            file.write(target + '\\n')
        return ''
    raise AssertionError('unexpected_docker_command')

def sink(event):
    if event.detail == 'image_build_waiting':
        (root / (target + '-waiting')).touch()

artifact = build(sys.argv[3], root / target, plan, execute=execute,
                 sink=sink, lock_timeout=5)
print(artifact.model_dump_json())
"""


class ImageLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.tag = 'app:' + uuid.uuid4().hex + 'a' * 8

    def start(self, script, *args):
        process = subprocess.Popen(
            [sys.executable, '-c', script, *map(str, args)], cwd=ROOT,
            env=child_environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        self.addCleanup(cleanup)
        return process

    def wait_file(self, name):
        deadline = time.monotonic() + 5
        while not (self.root / name).exists():
            if time.monotonic() >= deadline:
                self.fail('child did not reach ' + name)
            time.sleep(0.01)

    def test_local_and_aws_wait_then_reuse_one_verified_image(self):
        fixture = ROOT / 'tests/fixtures/analyzer/v1'
        source = fixture / 'snapshot'
        mapping = json.loads((fixture / 'repo_map.json').read_text())
        mapping['commit'] = self.tag.removeprefix('app:')
        for target in ('aws', 'local'):
            plan = json.loads((ROOT / f'schemas/fixtures/v1/plan.{target}.json').read_text())
            plan.update(source_revision=mapping['commit'], image_tag=self.tag)
            (self.root / (target + '.json')).write_text(json.dumps(plan))
            patch_snapshot(source, mapping, plan, self.root / target)
        aws = self.start(BUILD_CHILD, self.root, 'aws', source)
        self.wait_file('building')
        local = self.start(BUILD_CHILD, self.root, 'local', source)
        self.wait_file('local-waiting')
        self.assertIsNone(local.poll())
        (self.root / 'release').touch()
        artifacts = []
        for process in (aws, local):
            output, error = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, error)
            artifacts.append(json.loads(output))
        self.assertEqual([a['target'] for a in artifacts], ['aws', 'local'])
        self.assertEqual({a['image'] for a in artifacts}, {self.tag})
        self.assertEqual({a['source_revision'] for a in artifacts}, {mapping['commit']})
        self.assertEqual((self.root / 'builds').read_text().splitlines(), ['aws'])

    def test_wait_timeout_is_bounded_and_closes_descriptor(self):
        with image_lock(self.tag):
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeFailure, '^image_build_wait_timeout$'):
                with image_lock(self.tag, timeout=0.05):
                    self.fail('held lock was acquired')
            self.assertLess(time.monotonic() - started, 1)
        with image_lock(self.tag, timeout=0):
            pass

    def test_invalid_wait_time_rejected(self):
        for value in (-1, True, None, float('inf'), float('nan')):
            with self.subTest(value=value), self.assertRaisesRegex(PolicyError, 'invalid_build_lock_timeout'):
                with image_lock(self.tag, timeout=value):
                    self.fail('invalid timeout accepted')

    def test_exception_releases_lock(self):
        with self.assertRaisesRegex(RuntimeError, 'test_failure'):
            with image_lock(self.tag):
                raise RuntimeError('test_failure')
        with image_lock(self.tag, timeout=0):
            pass

    def test_unrelated_image_does_not_wait(self):
        with image_lock(self.tag):
            with image_lock(self.tag + '-other', timeout=0):
                pass

    def test_killed_builder_releases_lock(self):
        owner = self.start("""
import sys, time
from pathlib import Path
from builder.runtime import image_lock
with image_lock(sys.argv[1]):
    Path(sys.argv[2]).touch()
    time.sleep(10)
""", self.tag, self.root / 'locked')
        self.wait_file('locked')
        owner.kill()
        owner.communicate(timeout=5)
        with image_lock(self.tag, timeout=0):
            pass


class LocalBuildDiagnosticTests(unittest.TestCase):
    def test_known_build_failures_are_public(self):
        for code in ('image_build_wait_timeout', 'image_tag_collision', 'command_failed'):
            self.assertEqual(public_failure_code('build', ValueError(code)), code)

    def test_arbitrary_source_or_secret_error_is_not_public(self):
        for error in (ValueError('source_instruction_private_canary'),
                      RuntimeError('image_tag_collision: private_canary'),
                      OSError('image_tag_collision')):
            self.assertEqual(public_failure_code('build', error), 'local_pipeline_failed')
        self.assertEqual(public_failure_code('start', ValueError('image_tag_collision')),
                         'local_pipeline_failed')
