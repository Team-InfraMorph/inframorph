"""Policy relations, transform boundaries, diagnostics and durable UI API."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from schemas import Intent, Plan
from policy_gate.gate import PolicyError, validate_intent, validate_plan
from policy_gate.rules import patch_rules
from policy_gate.structure import inspect_js, prisma_structure
from policy_gate.reporting import observe, result
from control_plane.app import create_app
from control_plane.runtime import LocalRuntime
from control_plane.b_bridge import DemoModules
from control_plane.policy_results import save, read
from tests import test_e_runtime

ROOT = Path(__file__).resolve().parents[1]


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.source = ROOT / 'tests/fixtures/analyzer/v1/snapshot'
        self.intent = json.loads((ROOT / 'schemas/fixtures/v1/intent.json').read_text())
        self.plan = json.loads((ROOT / 'schemas/fixtures/v1/plan.local.json').read_text())

    def test_db_claim_and_evidence_are_related(self):
        validate_intent(self.intent,self.source,self.intent['source_revision'])
        for change,code in [({'engine':'postgresql'},'db_provider_mismatch'),({'evidence':['src/server.js:1']},'db_evidence_unrelated')]:
            value=copy.deepcopy(self.intent);value['state'][0].update(change)
            with self.assertRaisesRegex(PolicyError,code): validate_intent(value,self.source,value['source_revision'])

    def test_plan_cannot_add_workers_or_override_secrets(self):
        validate_plan(self.intent,self.plan)
        for change in ({'secrets':['DATABASE_URL','NEW_SECRET']},{'config':{'STORAGE_DRIVER':'fs','NODE_OPTIONS':'--inspect'}}):
            with self.assertRaises(PolicyError): validate_plan(self.intent,self.plan|change)

    def test_omitted_db_is_blocked(self):
        value=self.intent|{'state':self.intent['state'][1:]}
        with self.assertRaisesRegex(PolicyError,'db_requirement_missing'): validate_intent(value,self.source,value['source_revision'])

    def test_prisma_preserves_models_defaults_and_directives(self):
        original=(self.source/'prisma/schema.prisma').read_bytes()
        provider,tokens,_=prisma_structure(original)
        transformed=original.replace(b'"sqlite"',b'"postgresql"')
        self.assertEqual(tokens,prisma_structure(transformed)[1])
        self.assertNotEqual(tokens,prisma_structure(transformed.replace(b'text String',b'text String @unique'))[1])
        self.assertEqual(tokens,prisma_structure(original+b'\n// model removal is forbidden\n')[1])

    def test_comments_and_strings_do_not_trigger_execution_rule(self):
        files={'src/a.js':b'// eval( child_process\nconst text = "eval( child_process";'}
        self.assertFalse(inspect_js(files)['src/a.js']['forbidden'])
        for code in [b"require('node:child_process')",b"globalThis['eval']('1')",b'const f = eval; f("1")']:
            self.assertTrue(inspect_js({'x.js':code})['x.js']['forbidden'])

    def test_ast_preserves_regex_and_directives(self):
        ast=inspect_js({'a.js':b'"use strict"; const x=/a/;', 'b.js':b'const x=/b/;'})
        self.assertNotEqual(ast['a.js']['normalized'],ast['b.js']['normalized'])

    def test_arbitrary_behavior_inside_allowed_file_is_blocked(self):
        fixture=test_e_runtime.GateTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.make_bundle({'src/storage.js': b'module.exports = {admin: true};\n'})
        from policy_gate.gate import validate_patch
        with self.assertRaisesRegex(PolicyError,'patch_behavior_changed'): validate_patch(fixture.original,fixture.bundle,test_e_runtime.plan())

    def test_unsupported_config_and_parser_failure_are_not_pass(self):
        reports=[]
        with observe(reports.append), self.assertRaises(PolicyError):
            validate_intent(self.intent|{'config':{'CUSTOM':'normal'}},self.source,self.intent['source_revision'])
        self.assertEqual(reports[0]['decision'],'UNSUPPORTED')
        with patch('policy_gate.structure.subprocess.run', side_effect=OSError()), self.assertRaisesRegex(PolicyError,'unavailable'):
            inspect_js({'x.js':b'const a=1;'})
        self.assertEqual(result('patch',PolicyError('javascript_parser_unavailable'))['decision'],'ERROR')

    def test_error_details_are_not_reflected(self):
        error=ValueError('password=private')
        error.path='../../.env';error.line=-1
        report=result('patch',error)
        self.assertNotIn('private',json.dumps(report));self.assertIsNone(report['path']);self.assertIsNone(report['line'])


class PolicyApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        self.runtime=LocalRuntime(root=self.root/'runtime',b_modules=DemoModules())
        self.app=create_app(db_path=self.root/'db.sqlite',runtime=self.runtime)
        self.addCleanup(self.app.state.store.close)
        self.client=TestClient(self.app);self.addCleanup(self.client.close)
        self.project=self.client.post('/api/projects',json={'repo_url':'https://github.com/Team-InfraMorph/demo-app','targets':['local']}).json()
        self.did=self.app.state.store.begin_deploy(self.project['project_id'])

    def test_unknown_and_not_checked_are_distinct(self):
        self.assertEqual(self.client.get('/api/deployments/missing/policy').status_code,404)
        self.assertEqual(self.client.get(f'/api/deployments/{self.did}/policy').json(),{'results':[]})

    def test_target_attempt_and_failure_survive_reopen(self):
        from control_plane.db import Store
        save(self.app.state.store,self.did,'local',0,result('patch',PolicyError('patch_behavior_changed')))
        save(self.app.state.store,self.did,'local',1,result('patch'))
        save(self.app.state.store,self.did,'aws',0,result('plan'))
        reopened=Store(self.root/'db.sqlite');self.addCleanup(reopened.close)
        rows=read(reopened,self.did)['results']
        self.assertEqual([(r['target'],r['attempt'],r['decision']) for r in rows],[('local',0,'BLOCK'),('local',1,'PASS'),('aws',0,'PASS')])
        self.assertEqual(rows,self.client.get(f'/api/deployments/{self.did}/policy').json()['results'])

    def test_initial_source_failure_is_recorded_without_patch(self):
        from analyzer.source_policy import SourcePolicyError
        with patch('control_plane.runtime.validate_demo_intent',side_effect=SourcePolicyError('unreviewed_runtime_source')):
            with self.assertRaises(Exception): self.runtime.analyze(self.app.state.store,self.app.state.store.get_deployment(self.did))
        rows=self.client.get(f'/api/deployments/{self.did}/policy').json()['results']
        self.assertEqual(rows[-1]['decision'],'UNSUPPORTED')

    def test_repeated_identical_check_is_stored_once_but_changes_are_kept(self):
        delivery=result('intent')
        for _ in range(3): save(self.app.state.store,self.did,'local',0,delivery)
        # A different decision, stage, target or attempt is never collapsed into the duplicate.
        save(self.app.state.store,self.did,'local',0,result('intent',PolicyError('evidence_file_missing')))
        save(self.app.state.store,self.did,'local',0,result('patch'))
        save(self.app.state.store,self.did,'local',1,result('intent'))
        save(self.app.state.store,self.did,'aws',0,result('intent'))
        rows=self.client.get(f'/api/deployments/{self.did}/policy').json()['results']
        self.assertEqual([(r['target'],r['attempt'],r['stage'],r['decision']) for r in rows],
                         [('local',0,'intent','PASS'),('local',0,'intent','BLOCK'),('local',0,'patch','PASS'),
                          ('local',1,'intent','PASS'),('aws',0,'intent','PASS')])

    def test_analysis_records_real_intent_and_plan_checks(self):
        self.runtime.analyze(self.app.state.store,self.app.state.store.get_deployment(self.did))
        rows=self.client.get(f'/api/deployments/{self.did}/policy').json()['results']
        self.assertEqual([r['stage'] for r in rows],['source','intent','profile','plan'])
        self.assertTrue(all(r['decision']=='PASS' for r in rows))
