"""Direct evidence contracts, using real reviewed source plus parser boundary cases."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from analyzer.source_policy import validate_demo_intent, validate_demo_plan
from builder.runtime import check_build_profile
from code_patch import patch_snapshot
from policy_gate.gate import PolicyError, validate_intent, validate_patch, validate_plan
from policy_gate.reporting import observe, error_detail
from policy_gate.structure import inspect_js, prisma_provider_lines, json_value_lines
from schemas import RepoMap

ROOT = Path(__file__).resolve().parents[1]


class EvidenceRuleTests(unittest.TestCase):
    def setUp(self):
        self.source = ROOT/'tests/fixtures/analyzer/v2/snapshot'
        self.intent = json.loads((ROOT/'schemas/fixtures/v2/intent.json').read_text())
        self.mapping = RepoMap.model_validate_json((self.source.parent/'repo_map.json').read_text())
        self.plan = json.loads((ROOT/'schemas/fixtures/v2/plan.local.json').read_text())

    def check_failure(self, value, code, rule_id):
        # The reviewed source boundary passes; these failures are not hash rejects.
        validate_demo_intent(value, self.source, self.mapping)
        reports = []
        with observe(reports.append), self.assertRaisesRegex(PolicyError, '^'+code+'$') as caught:
            validate_intent(value, self.source, self.mapping.commit)
        record = next(r for r in reports[-1]['rules'] if r['rule_id'] == rule_id)
        self.assertEqual(record['decision'], 'BLOCK')
        self.assertEqual(record['reason_code'], code)
        details = caught.exception.diagnostics
        for key, item in details.items():
            self.assertEqual(record['evidence'][key], item)
        return details

    def test_datasource_url_and_closing_brace_are_not_provider_evidence(self):
        for line in (7, 8):
            value = copy.deepcopy(self.intent)
            value['state'][0]['evidence'] = [f'prisma/schema.prisma:{line}']
            with self.subTest(line=line):
                details = self.check_failure(value, 'db_provider_evidence_missing', 'I-003')
                self.assertEqual(details['missing_roles'], ['db_provider'])
                self.assertEqual(details['required_anchors'][0]['lines'], [6])
                self.assertEqual(details['allowed_evidence_paths'], ['/state/0/evidence'])
                self.assertEqual(details['observed'], 'sqlite')

    def test_worker_needs_registered_command_and_start_anchor(self):
        cases = [(['src/worker.js:1'], 'worker_command_evidence_missing', ['worker_command','worker_start']),
                 (['package.json:12'], 'worker_start_evidence_missing', ['worker_start']),
                 (['src/worker.js:21'], 'worker_command_evidence_missing', ['worker_command']),
                 (['package.json:12','src/worker.js:1'], 'worker_start_evidence_missing', ['worker_start'])]
        for references, code, missing in cases:
            value = copy.deepcopy(self.intent)
            value['workloads'][1]['evidence'] = references
            with self.subTest(references=references):
                details = self.check_failure(value, code, 'I-002')
                self.assertEqual(details['missing_roles'], missing)
                self.assertEqual([a['role'] for a in details['required_anchors']], ['worker_command','worker_start'])
                self.assertEqual(details['required_anchors'][0]['lines'], [12])
                self.assertEqual(details['required_anchors'][1]['lines'], [21,22])
                self.assertEqual(details['allowed_evidence_paths'], ['/workloads/1/evidence'])

    def test_direct_tick_and_timer_are_alternative_start_anchors(self):
        for line in (21, 22):
            value = copy.deepcopy(self.intent)
            value['workloads'][1]['evidence'] = ['package.json:12', f'src/worker.js:{line}']
            validate_demo_intent(value, self.source, self.mapping)
            validate_intent(value, self.source, self.mapping.commit)

    def test_approved_evidence_continues_through_patch_and_build_input(self):
        validate_demo_intent(self.intent, self.source, self.mapping)
        validate_intent(self.intent, self.source, self.mapping.commit)
        validate_demo_plan(self.plan, self.mapping)
        validate_plan(self.intent, self.plan)
        with tempfile.TemporaryDirectory() as folder:
            bundle = Path(folder).resolve()/'bundle'
            patch_snapshot(self.source, self.mapping, self.plan, bundle)
            artifact = validate_patch(self.source, bundle, self.plan)
            check_build_profile(artifact.files)

    def test_no_worker_path_remains_valid(self):
        source = ROOT/'tests/fixtures/analyzer/v1/snapshot'
        value = json.loads((ROOT/'schemas/fixtures/v1/intent.json').read_text())
        validate_intent(value, source, value['source_revision'])

    def test_old_unrelated_evidence_errors_remain_distinct(self):
        value = copy.deepcopy(self.intent)
        value['state'][0]['evidence'] = ['src/server.js:22']
        self.check_failure(value, 'db_evidence_unrelated', 'I-003')
        value = copy.deepcopy(self.intent)
        value['workloads'][1]['evidence'] = ['src/server.js:22']
        self.check_failure(value, 'worker_evidence_unrelated', 'I-002')


class EvidenceParserTests(unittest.TestCase):
    def test_provider_anchor_uses_tokens_not_multiline_span_or_other_blocks(self):
        data = b'''generator client { provider = "prisma-client-js" }
datasource db {
  // provider = "sqlite"
  provider
  // citation on this line is not a provider token
  =
  "sqlite"
  url = env("DATABASE_URL")
}
model Note { id Int @id }
'''
        self.assertEqual(prisma_provider_lines(data), [4,7])

    def test_command_anchor_uses_exact_json_path_and_value_token(self):
        data = b'''{
 "description": "node src/worker.js",
 "scripts": {
   "worker":
     "node src/worker.js",
   "other": "node src/worker.js"
 }
}'''
        self.assertEqual(json_value_lines(data, ('scripts','worker')), [5])
        self.assertEqual(json_value_lines(data, ('worker',)), [])
        self.assertEqual(json_value_lines(b'{"scripts":{"work\\u0065r":"node src/worker.js"}}', ('scripts','worker')), [1])

    def test_ambiguous_or_malformed_json_cannot_provide_anchor(self):
        for data in (b'{"scripts":{"worker":"one","worker":"two"}}', b'{"scripts":',
                     b'{"scripts":{"worker":"one"}} trailing'):
            with self.subTest(data=data), self.assertRaisesRegex(PolicyError, '^package_invalid$'):
                json_value_lines(data, ('scripts','worker'))

    def starts(self, source):
        return inspect_js({'src/worker.js': source})['src/worker.js']['worker_starts']

    def test_worker_start_excludes_nested_calls_strings_comments_and_shadowing(self):
        cases = [b'function tick(){}; const text="tick()"; // setInterval(tick,10)\n',
                 b'function tick(){}; function unused(){ tick(); }',
                 b'function tick(){}; if(false) tick();',
                 b'const name="tick"; setInterval(name, 10);',
                 b'function tick(){}; function setInterval(){}; setInterval(tick,10);',
                 b'function tick(){}; tick = other; tick();',
                 b'function tick(){}; ({tick} = other); tick();',
                 b'function tick(){}; globalThis.setInterval = fake; setInterval(tick,10);',
                 b'function tick(){}; const {setInterval} = fake; setInterval(tick,10);',
                 b'function tick(){}; setInterval(() => tick(),10);']
        for source in cases:
            with self.subTest(source=source):
                self.assertEqual(self.starts(source), [])

    def test_multiline_start_uses_callee_and_callback_not_gaps(self):
        source = b'async function tick(){}\nconst timer = setInterval(\n // not evidence\n tick,\n 10_000\n);\ntick();\n'
        self.assertEqual([item['lines'] for item in self.starts(source)], [[2,4],[7]])

    def test_location_metadata_does_not_change_normalized_ast(self):
        parsed = inspect_js({'a.js': b'function tick(){}\ntick();',
                             'b.js': b'// leading comment\nfunction tick() {}\n\ntick();'})
        self.assertEqual(parsed['a.js']['normalized'], parsed['b.js']['normalized'])

    def test_parser_unavailability_is_error_and_invalid_js_is_block(self):
        with patch('policy_gate.structure.subprocess.run', side_effect=OSError()), self.assertRaises(PolicyError) as caught:
            inspect_js({'src/worker.js': b'tick();'})
        self.assertEqual(error_detail(caught.exception)['decision'], 'ERROR')
        parsed = inspect_js({'src/worker.js': b'function {'})
        self.assertEqual(parsed['src/worker.js']['error'], 'javascript_syntax_invalid')
        self.assertEqual(error_detail(PolicyError('javascript_syntax_invalid'))['decision'], 'BLOCK')


if __name__ == '__main__':
    unittest.main()
