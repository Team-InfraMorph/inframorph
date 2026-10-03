"""Executable walkthroughs and frozen documentation contract for policy 1.0.0."""
import copy
import hashlib
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError
from schemas import Intent, RepoMap
from policy_gate.catalog import identity
from policy_gate.gate import PolicyError, validate_intent, validate_patch, validate_plan
from policy_gate.reporting import observe
from analyzer.source_policy import SourcePolicyError, validate_demo_intent, validate_demo_plan
from builder.runtime import check_build_profile
from code_patch import patch_snapshot
from code_patch.runner import make_diff
from policy_gate.gate import read_tree
from scripts.check_policy_catalog import validate_baselines
from tests import test_e_runtime

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT/'policy_gate/docs/1.0.0/revisions/3'


class WalkthroughTests(unittest.TestCase):
    def setUp(self):
        self.source = ROOT/'tests/fixtures/analyzer/v1/snapshot'
        self.intent = json.loads((ROOT/'schemas/fixtures/v1/intent.json').read_text())
        self.plan = json.loads((ROOT/'schemas/fixtures/v1/plan.local.json').read_text())
        self.mapping = RepoMap.model_validate_json((ROOT/'tests/fixtures/analyzer/v1/repo_map.json').read_text())
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def inspect(self, fn, failure=None):
        rows = []
        with observe(rows.append):
            if failure:
                with self.assertRaisesRegex((PolicyError, SourcePolicyError), '^'+failure+'$'):
                    fn()
            else:
                fn()
        self.assertTrue(rows)
        return rows[-1], {r['rule_id']: r for r in rows[-1]['rules']}

    def repaired_intent(self):
        self.inspect(lambda: validate_demo_intent(self.intent, self.source, self.mapping))
        row, rules = self.inspect(lambda: validate_intent(self.intent, self.source, self.mapping.commit))
        self.assertTrue(row['complete'])
        self.assertEqual(row['decision'], 'PASS')
        self.assertTrue(all(r['decision'] in {'PASS', 'NOT_APPLICABLE'} for r in rules.values()))
        return row

    def test_documented_complete_intent_is_valid(self):
        body = (DOCS/'rules/I-000.md').read_text()
        example = json.loads(re.search(r'```json\n(.*?)\n```', body, re.S)[1])
        self.assertEqual(example, self.intent)
        self.intent = example
        self.repaired_intent()

    def test_documented_schema_boundaries(self):
        minimal = {k:v for k,v in self.intent.items() if k in {'source_revision','app','runtime','workloads'}}
        parsed = Intent.model_validate(minimal)
        self.assertEqual((parsed.schema_version, parsed.state, parsed.config, parsed.unknowns), ('1.0.0', [], {}, []))
        for change in ({'state':None}, {'unexpected':True}, {'source_revision':'ABC'}, {'runtime':'python'}, {'workloads':[]}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                Intent.model_validate(self.intent | change)
        for change in ({'port':0}, {'command':'node app.js'}, {'unexpected':True}):
            value = copy.deepcopy(self.intent)
            value['workloads'][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValidationError):
                Intent.model_validate(value)
        value = self.intent | {'unknowns':['DB not confirmed']}
        Intent.model_validate(value)  # Schema-valid, but explicitly blocked by I-000.
        _, rules = self.inspect(lambda:validate_intent(value,self.source,self.mapping.commit), 'unresolved_intent')
        self.assertEqual(rules['I-000']['decision'], 'BLOCK')

    def test_missing_evidence_then_repair(self):
        value = copy.deepcopy(self.intent)
        value['workloads'][0]['evidence'] = ['src/missing.js:1']
        failed, rules = self.inspect(lambda:validate_intent(value,self.source,self.mapping.commit), 'evidence_file_missing')
        self.assertEqual(rules['I-001']['decision'], 'BLOCK')
        self.assertEqual(rules['I-006']['decision'], 'NOT_RUN')
        self.assertIn('src/missing.js:1', rules['I-001']['evidence']['references'])
        self.inspect(lambda:validate_demo_intent(value,self.source,self.mapping), 'non_source_evidence')
        repaired = self.repaired_intent()
        self.assertNotEqual(failed['execution_id'], repaired['execution_id'])
        self.assertEqual(failed['decision'], 'BLOCK')

    def test_db_analysis_then_approved_transform(self):
        value = copy.deepcopy(self.intent)
        value['state'][0]['engine'] = 'postgresql'
        _, rules = self.inspect(lambda:validate_intent(value,self.source,self.mapping.commit), 'db_provider_mismatch')
        self.assertEqual(rules['I-003']['evidence'], dict(path='prisma/schema.prisma', observed='sqlite', claimed=['postgresql']))
        self.assertEqual(rules['I-004']['decision'], 'NOT_RUN')
        self.inspect(lambda:validate_demo_intent(value,self.source,self.mapping), 'intent_source_mismatch')
        self.repaired_intent()
        validate_demo_plan(self.plan,self.mapping)
        validate_plan(self.intent,self.plan)
        bundle = self.root/'approved'
        patch_snapshot(self.source,self.mapping,self.plan,bundle)
        artifact = validate_patch(self.source,bundle,self.plan)
        self.assertIn(b'"postgresql"', artifact.files['prisma/schema.prisma'])
        check_build_profile(artifact.files)
        self.assertEqual(self.intent['state'][0]['engine'],'sqlite')

    def test_unrelated_patch_then_regenerate(self):
        approved = self.root/'approved'
        patch_snapshot(self.source,self.mapping,self.plan,approved)
        artifact = validate_patch(self.source,approved,self.plan)
        fixture = test_e_runtime.GateTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.original = self.source
        bad = dict(artifact.files)
        bad['src/storage.js'] += b'\nmodule.exports.admin = true;\n'
        fixture.make_bundle(bad)  # Consistent manifest+diff: reach the semantic rule.
        manifest_path = fixture.bundle/'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['source_revision'] = self.mapping.commit
        # Keep the generator's newline-aware diff format for real source files.
        diff, changes = make_diff(read_tree(self.source, filter_source=True)[0], bad)
        (fixture.bundle/'patch.diff').write_text(diff)
        manifest['diff_sha256'] = hashlib.sha256(diff.encode()).hexdigest()
        manifest['changes'] = changes
        manifest_path.write_text(json.dumps(manifest))
        failed, rules = self.inspect(lambda:validate_patch(self.source,fixture.bundle,self.plan), 'patch_behavior_changed')
        for name in ('P-001','P-006','P-007','P-002','P-003'):
            self.assertEqual(rules[name]['decision'],'PASS',name)
        self.assertEqual(rules['P-004']['path'],'src/storage.js')
        self.assertEqual(rules['P-005']['decision'],'NOT_RUN')
        repaired_bundle = self.root/'regenerated'
        patch_snapshot(self.source,self.mapping,self.plan,repaired_bundle)
        repaired, repaired_rules = self.inspect(lambda:validate_patch(self.source,repaired_bundle,self.plan))
        self.assertEqual(repaired_rules['P-004']['decision'],'PASS')
        self.assertTrue(repaired['complete'])
        check_build_profile(validate_patch(self.source,repaired_bundle,self.plan).files)
        self.assertNotEqual(failed['execution_id'], repaired['execution_id'])
        self.assertEqual(failed['decision'],'BLOCK')


class FrozenBaselineTests(unittest.TestCase):
    def test_repository_baseline(self):
        baseline = json.loads((ROOT/'policy_gate/baselines/1.0.0.json').read_text())
        self.assertEqual(baseline['document_revisions'], [1,2,3])
        self.assertEqual(identity()['document_revision'], 3)
        validate_baselines()

    def test_baseline_detects_document_and_active_policy_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root/'policy_gate/docs/1.0.0/revisions/3'
            folder.mkdir(parents=True)
            doc = folder/'overview.md'
            doc.write_text('fixed document')
            meta = dict(version='1.0.0',policy_digest='a',rules_digest='b',implementation_digest='c')
            baseline = dict(version='1.0.0',identity=meta,document_revisions=[3],
                            documents={str(doc.relative_to(root)):hashlib.sha256(doc.read_bytes()).hexdigest()})
            records = root/'policy_gate/baselines'
            records.mkdir()
            (records/'1.0.0.json').write_text(json.dumps(baseline))
            validate_baselines(root, meta)
            for key in ('policy_digest','rules_digest','implementation_digest'):
                with self.subTest(key=key), self.assertRaisesRegex(AssertionError,'frozen policy identity'):
                    validate_baselines(root, meta | {key:'changed'})
            doc.write_text('modified')
            with self.assertRaisesRegex(AssertionError,'frozen document changed'):
                validate_baselines(root,meta)
            doc.unlink()
            with self.assertRaisesRegex(AssertionError,'inventory'):
                validate_baselines(root,meta)
            doc.write_text('fixed document')
            (folder/'extra.md').write_text('new')
            with self.assertRaisesRegex(AssertionError,'inventory'):
                validate_baselines(root,meta)


class ExecutionFlowTests(unittest.TestCase):
    def test_local_failure_prevents_remote_commands(self):
        from control_plane.db import Store
        from control_plane.orchestrator import run_deployment, LOCAL_TEST_FAILED
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root/'control.db')
            self.addCleanup(store.close)
            project = store.create_project('https://github.com/o/r','main',['local','aws','onprem'])
            did = store.begin_deploy(project['project_id'])
            event = json.dumps(dict(deployment_id=did,ts='2026-10-03T00:00:00Z',target='local',step='health',status='fail'))
            commands = {'local':[sys.executable,'-c',f'print({event!r})']}
            for target in ('aws','onprem'):
                commands[target] = [sys.executable,'-c',f'from pathlib import Path; Path({str(root/target)!r}).touch()']
            result = run_deployment(store,did,commands)
            self.assertEqual(result['local'].status.value,'FAILED')
            for target in ('aws','onprem'):
                self.assertFalse((root/target).exists(),target)
                self.assertEqual(result[target].status.value,'FAILED')
            events = [row['event'] for row in store.list_events(did)]
            self.assertEqual({e['target'] for e in events if e.get('detail')==LOCAL_TEST_FAILED},{'aws','onprem'})
