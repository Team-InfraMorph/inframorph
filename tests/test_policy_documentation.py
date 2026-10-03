"""Executable evidence for current documentation; no changes to gate semantics."""
import ast
import copy
import hashlib
import importlib
import json
import re
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from policy_gate.catalog import identity, document, release
from policy_gate.gate import PolicyError, validate_intent, validate_patch, validate_plan
from policy_gate.reporting import observe
from analyzer.source_policy import validate_demo_intent, validate_demo_plan
from schemas import RepoMap
from tests import test_e_runtime
from tests.test_e_runtime import plan
from builder.runtime import check_build_profile

ROOT=Path(__file__).resolve().parents[1]
ACTIVE_VERSION=json.loads((ROOT/'policy_gate/active.json').read_text())['version']
DOCS=ROOT/'policy_gate/docs'/ACTIVE_VERSION

class DocumentationContract(unittest.TestCase):
    def test_one_document_set_and_unchanged_rule_contract(self):
        baseline=json.loads((DOCS/'review.json').read_text())['baseline']
        self.assertEqual(identity()['rules_digest'],baseline['rules_digest'])
        self.assertFalse((DOCS/'revisions').exists())
        self.assertNotIn('document_revision',identity())
        self.assertNotIn('document_revisions',release())
        for name,digest in baseline['behavior_files'].items():
            self.assertEqual(hashlib.sha256((ROOT/name).read_bytes()).hexdigest(),digest,name)
        for path in DOCS.rglob('*.md'):
            self.assertNotIn('문서 revision',path.read_text(),path)

    def test_all_rules_have_sections_and_real_test_methods(self):
        review=json.loads((DOCS/'review.json').read_text())
        self.assertEqual(set(review['rule_ids']),{r['id'] for r in release()['rules']})
        for rule in release()['rules']:
            body=(DOCS/f"rules/{rule['id']}.md").read_text()
            self.assertTrue(body.startswith(f"# {rule['id']} · {rule['title']}\n"))
            self.assertNotIn("rule('",body)
            for section in review['required_sections']:self.assertIn('{#'+section+'}',body)
            for ref in review['tests'][rule['id']]:
                module,cls,method=ref.rsplit('.',2)
                self.assertTrue(callable(getattr(getattr(importlib.import_module(module),cls),method)),ref)
                self.assertIn(ref,body)

    def test_document_links_and_stable_sections(self):
        for path in DOCS.rglob('*.md'):
            body=path.read_text();ids=re.findall(r'^## .+ \{#([a-z0-9-]+)\}$',body,re.M)
            self.assertEqual(len(ids),len(set(ids)),path)
            for href in re.findall(r'\[[^\]]+\]\(([^)]+)\)',body):
                if href.startswith('https://'):continue
                part,_,section=href.partition('#');target=(path.parent/part).resolve() if part else path
                self.assertTrue(target.is_relative_to(DOCS),href)
                self.assertTrue(target.is_file(),(path,href))
                if section:self.assertIn('{#'+section+'}',target.read_text(),href)

    def test_read_api_uses_policy_version_and_accepts_old_links(self):
        from control_plane.app import create_app
        import tempfile
        with tempfile.TemporaryDirectory() as temp, TestClient(create_app(db_path=Path(temp)/'cp.db')) as client:
            current=client.get('/api/policies/1.0.0').json()
            self.assertNotIn('document_revisions',current)
            self.assertTrue(all(d['version']=='1.0.0' and 'revision' not in d for d in current['documents']))
            for revision in (1,4,99):
                old=client.get(f'/api/policies/1.0.0?revision={revision}').json()
                self.assertEqual(old,current)
            doc=client.get('/api/policies/1.0.0/documents/rules/I-000?revision=3').json()
            self.assertEqual(doc['source'],'policy_gate/docs/1.0.0/rules/I-000.md')
            self.assertIn('{#contract}',doc['body'])
            self.assertEqual(client.get('/api/policies/9.9.9').status_code,409)
            self.assertEqual(client.get('/api/policies/1.0.0/documents/missing').status_code,409)

    def test_document_only_edit_does_not_reactivate_or_queue_rechecks(self):
        import tempfile
        from policy_gate.catalog import index
        from control_plane.db import Store
        from control_plane import policy_lifecycle as life
        before=identity()
        changed=copy.deepcopy(index('1.0.0'));changed[0]['sha256']='f'*64
        with patch('policy_gate.catalog.index',return_value=changed):after=identity()
        for key in ('policy_digest','rules_digest','implementation_digest'):
            self.assertEqual(before[key],after[key],key)
        self.assertNotEqual(before['documents_digest'],after['documents_digest'])
        with tempfile.TemporaryDirectory() as temp:
            store=Store(Path(temp)/'cp.db')
            try:
                life.activate(store)
                events=store._one('SELECT COUNT(*) n FROM policy_events')['n']
                with patch('control_plane.policy_lifecycle.identity',return_value=after):life.activate(store)
                self.assertEqual(store._one('SELECT COUNT(*) n FROM policy_events')['n'],events)
                self.assertEqual(store._one('SELECT COUNT(*) n FROM policy_jobs')['n'],0)
            finally:store.close()

class DocumentedScenarios(unittest.TestCase):
    def setUp(self):
        self.source=ROOT/'tests/fixtures/analyzer/v1/snapshot'
        self.intent=json.loads((ROOT/'schemas/fixtures/v1/intent.json').read_text())
        self.plan=json.loads((ROOT/'schemas/fixtures/v1/plan.local.json').read_text())
        self.mapping=RepoMap.model_validate_json((ROOT/'tests/fixtures/analyzer/v1/repo_map.json').read_text())

    def inspect(self,fn):
        reports=[]
        with observe(reports.append):
            try:fn()
            except (PolicyError,ValueError):pass
        self.assertTrue(reports)
        return reports[-1],{r['rule_id']:r for r in reports[-1]['rules']}

    def bundle(self):
        fixture=test_e_runtime.GateTests();fixture.setUp();self.addCleanup(fixture.doCleanups);return fixture

    def test_evidence_boundaries(self):
        for citation,code in [('src/missing.js:1','evidence_file_missing'),('src/server.js:999999','evidence_line_missing')]:
            with self.subTest(citation=citation):
                value=copy.deepcopy(self.intent);value['workloads'][0]['evidence']=[citation]
                _,rules=self.inspect(lambda:validate_intent(value,self.source,value['source_revision']))
                self.assertEqual((rules['I-001']['decision'],rules['I-001']['reason_code']),('BLOCK',code))
                self.assertEqual(rules['I-006']['decision'],'NOT_RUN')
        empty=next(i for i,line in enumerate((self.source/'src/server.js').read_text().splitlines(),1) if not line.strip())
        value=copy.deepcopy(self.intent);value['workloads'][0]['evidence']=[f'src/server.js:{empty}']
        _,rules=self.inspect(lambda:validate_intent(value,self.source,value['source_revision']))
        self.assertEqual(rules['I-001']['decision'],'PASS')
        row,_=self.inspect(lambda:validate_demo_intent(value,self.source,self.mapping))
        self.assertEqual(row['reason_code'],'invalid_source_evidence')

    def test_db_failure_stops_following_rules(self):
        for change,code in [('provider','db_provider_mismatch'),('missing','db_requirement_missing')]:
            with self.subTest(change=change):
                value=copy.deepcopy(self.intent)
                if change=='provider':value['state'][0]['engine']='postgresql'
                else:value['state']=value['state'][1:]
                row,rules=self.inspect(lambda:validate_intent(value,self.source,value['source_revision']))
                self.assertEqual((row['decision'],rules['I-003']['reason_code']),('BLOCK',code))
                self.assertEqual(rules['I-004']['decision'],'NOT_RUN');self.assertEqual(rules['I-002']['decision'],'NOT_RUN')

    def test_storage_omission_is_blocked_by_source_profile(self):
        value=copy.deepcopy(self.intent);value['state']=[s for s in value['state'] if s['kind']!='persistent_files']
        _,rules=self.inspect(lambda:validate_intent(value,self.source,value['source_revision']))
        self.assertEqual(rules['I-004']['decision'],'NOT_APPLICABLE')
        row,_=self.inspect(lambda:validate_demo_intent(value,self.source,self.mapping))
        self.assertEqual((row['decision'],row['reason_code']),('BLOCK','intent_source_mismatch'))

    def test_storage_evidence_branches(self):
        value=copy.deepcopy(self.intent)
        storage=next(s for s in value['state'] if s['kind']=='persistent_files')
        storage['evidence']=['src/server.js:1']
        row,rules=self.inspect(lambda:validate_intent(value,self.source,value['source_revision']))
        self.assertEqual(rules['I-004']['reason_code'],'storage_evidence_unrelated')
        storage['path']='other'
        row,_=self.inspect(lambda:validate_intent(value,self.source,value['source_revision']))
        self.assertEqual(row['decision'],'UNSUPPORTED')

    def test_worker_omission(self):
        source=ROOT/'tests/fixtures/analyzer/v2/snapshot'
        value=json.loads((ROOT/'schemas/fixtures/v2/intent.json').read_text())
        value['workloads']=[w for w in value['workloads'] if w['kind']!='worker']
        _,rules=self.inspect(lambda:validate_intent(value,source,value['source_revision']))
        self.assertEqual((rules['I-002']['decision'],rules['I-002']['reason_code']),('BLOCK','worker_requirement_missing'))

    def test_config_decision_branches(self):
        for config,expected in [({'CUSTOM':'normal'},'UNSUPPORTED'),({'DATABASE_URL':'example'},'BLOCK'),({'PORT':'99999'},'PASS')]:
            with self.subTest(config=config):
                row,rules=self.inspect(lambda:validate_intent(self.intent|{'config':config},self.source,self.intent['source_revision']))
                self.assertEqual(rules['I-006']['decision'],expected)

    def test_unreadable_source_classification(self):
        row,_=self.inspect(lambda:validate_intent(self.intent,self.source/'nonexistent',self.intent['source_revision']))
        self.assertEqual((row['decision'],row['reason_code']),('BLOCK','unreadable_source'))

    def test_profile_and_plan_rules(self):
        validate_demo_plan(self.plan,self.mapping);validate_plan(self.intent,self.plan)
        bad=self.plan|{'secrets':['DATABASE_URL','NEW_SECRET']}
        _,rules=self.inspect(lambda:validate_plan(self.intent,bad))
        self.assertEqual(rules['L-001']['decision'],'PASS');self.assertEqual(rules['L-002']['reason_code'],'plan_secret_mismatch')
        bad=copy.deepcopy(self.plan);bad['services'][0]['public']=False
        _,rules=self.inspect(lambda:validate_plan(self.intent,bad))
        self.assertEqual(rules['L-001']['reason_code'],'schema_invalid');self.assertEqual(rules['L-002']['decision'],'NOT_RUN')

    def test_parser_failure_ledger(self):
        fixture=self.bundle()
        with patch('policy_gate.structure.subprocess.run',side_effect=OSError()):
            row,rules=self.inspect(lambda:validate_patch(fixture.original,fixture.bundle,plan()))
        self.assertEqual((row['decision'],rules['P-006']['reason_code']),('ERROR','javascript_parser_unavailable'))
        self.assertEqual(rules['P-002']['decision'],'NOT_RUN')

    def test_unchanged_js_still_calls_parser(self):
        fixture=self.bundle();fixture.make_bundle({'src/storage.js':b'module.exports = {};\n'})
        import policy_gate.structure as structure
        real=structure.inspect_js;calls=[]
        def failing(files):
            calls.append(files)
            if len(calls)>1:raise PolicyError('javascript_parser_unavailable')
            return real(files)
        with patch('policy_gate.structure.inspect_js',side_effect=failing),patch('policy_gate.rules.inspect_js',side_effect=failing):
            row,rules=self.inspect(lambda:validate_patch(fixture.original,fixture.bundle,plan()))
        self.assertGreater(len(calls),1);self.assertEqual(rules['P-004']['decision'],'ERROR')

    def test_secret_pattern_ledger(self):
        fixture=self.bundle();fixture.make_bundle({'src/storage.js':b'// AKIA'+b'A'*16+b'\nmodule.exports = {};\n'})
        _,rules=self.inspect(lambda:validate_patch(fixture.original,fixture.bundle,plan()))
        self.assertEqual((rules['P-007']['decision'],rules['P-007']['reason_code']),('BLOCK','secret_in_source'))

    def test_diff_failure_ledger(self):
        fixture=self.bundle();manifest=json.loads((fixture.bundle/'manifest.json').read_text())
        (fixture.bundle/'patch.diff').write_bytes(b'')
        manifest['diff_sha256']=hashlib.sha256(b'').hexdigest();(fixture.bundle/'manifest.json').write_text(json.dumps(manifest))
        _,rules=self.inspect(lambda:validate_patch(fixture.original,fixture.bundle,plan()))
        self.assertEqual(rules['P-002']['reason_code'],'diff_source_mismatch')

    def transformed(self):
        from code_patch import patch_snapshot
        import tempfile
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        bundle=Path(temp.name).resolve()/'bundle';patch_snapshot(self.source,self.mapping,self.plan,bundle)
        return bundle,validate_patch(self.source,bundle,self.plan)

    def test_prisma_transform_ledger(self):
        _,artifact=self.transformed()
        self.assertIn(b'postgresql',artifact.files['prisma/schema.prisma'])
        from policy_gate.rules import patch_rules
        from policy_gate.structure import inspect_js
        original={'prisma/schema.prisma':(self.source/'prisma/schema.prisma').read_bytes()}
        changed=dict(original);changed['prisma/schema.prisma']=original['prisma/schema.prisma'].replace(b'"sqlite"',b'"postgresql"')+b'\nmodel Added { id Int @id }\n'
        with self.assertRaisesRegex(PolicyError,'prisma_structure_changed'):
            from schemas import Plan
            patch_rules(original,changed,Plan.model_validate(self.plan),list(original),inspect_js(changed))

    def test_dependency_transform_ledger(self):
        _,artifact=self.transformed()
        from policy_gate.rules import patch_rules
        from schemas import Plan
        old={'package.json':(self.source/'package.json').read_bytes()}
        package=json.loads(artifact.files['package.json']);package['scripts']['extra']='echo example'
        with self.assertRaisesRegex(PolicyError,'dependency_change_forbidden'):
            patch_rules(old,{'package.json':json.dumps(package).encode()},Plan.model_validate(self.plan),['package.json'],{})

    def test_build_profile_boundaries(self):
        _,artifact=self.transformed();check_build_profile(artifact.files)
        changed=dict(artifact.files);changed['package-lock.json']+=b'\n'
        row,rules=self.inspect(lambda:check_build_profile(changed))
        self.assertEqual((row['decision'],rules['X-001']['reason_code']),('BLOCK','unreviewed_dependency_lock'))
