"""Run the exact contracts and record walkthrough published in policy version 1.0.0."""
import copy
import json
import re
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError
from analyzer.source_policy import SourcePolicyError, validate_demo_intent, validate_demo_plan
from code_patch import patch_snapshot
from control_plane.db import Store, Status
from control_plane import policy_lifecycle as life
from control_plane.policy_results import check, read
from policy_gate.gate import PolicyError, validate_patch, validate_plan
from policy_gate.reporting import observe
from schemas import Plan, RepoMap

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT/'policy_gate/docs/1.0.0'
SOURCE = ROOT/'tests/fixtures/analyzer/v1/snapshot'
MAPPING = RepoMap.model_validate_json((ROOT/'tests/fixtures/analyzer/v1/repo_map.json').read_text())
INTENT = json.loads((ROOT/'schemas/fixtures/v1/intent.json').read_text())
PLAN = json.loads((ROOT/'schemas/fixtures/v1/plan.local.json').read_text())
TEST_REF = 'tests.test_policy_documentation_contracts.VersionContractTests.test_record_walkthrough'


def example(name):
    return json.loads(re.search(r'```json\n(.*?)\n```', (DOCS/name).read_text(), re.S)[1])


def record_walkthrough(folder):
    """Use real gate/storage methods; abbreviate only random IDs in the reading excerpt."""
    store = Store(folder/'records.sqlite')
    try:
        project = store.create_project('https://github.com/Team-InfraMorph/demo-app', 'main', ['local'])
        failed_id = store.begin_deploy(project['project_id'], revision=MAPPING.commit)
        invalid = copy.deepcopy(INTENT)
        invalid['workloads'][0]['evidence'] = ['src/missing.js:1']
        try:
            check(store, failed_id, 'local', 'source', lambda: validate_demo_intent(invalid, SOURCE, MAPPING))
        except SourcePolicyError as error:
            assert str(error) == 'non_source_evidence'
        else:
            raise AssertionError('expected source failure')
        before = copy.deepcopy(read(store, failed_id)['results'][0])
        store.set_status(failed_id, Status.FAILED)
        fixed_id = store.begin_deploy(project['project_id'], revision=MAPPING.commit)
        check(store, fixed_id, 'local', 'source', lambda: validate_demo_intent(INTENT, SOURCE, MAPPING))
        passed = read(store, fixed_id)['results'][0]
        life.failure_review(store, failed_id, before['execution_id'], dict(
            classification='violation', summary='승인된 소스의 실제 근거로 분석을 다시 생성했습니다.',
            regression_test=TEST_REF, resolved_execution_id=passed['execution_id']))
        failed = read(store, failed_id)['results'][0]
        history = life.details(store, failed_id)
        fixed_history = life.details(store, fixed_id)
        assert before == failed
        assert failed['execution_id'] != passed['execution_id']
        assert failed['binding']['input_sha256'] != passed['binding']['input_sha256']
        assert failed['policy_digest'] == passed['policy_digest']
        aliases = {failed['execution_id']:'검사-A', passed['execution_id']:'검사-B'}
        def excerpt(record):
            fields = ('family','version','target','attempt','stage','checkpoint','decision','complete','required_rules','evaluated_rules','reason_code')
            result = {key:record[key] for key in fields}
            result['execution_id'] = aliases[record['execution_id']]
            result['rules'] = [{key:r[key] for key in ('rule_id','revision','decision','reason_code','evidence')} for r in record['rules']]
            return result
        review = copy.deepcopy(history['failure_reviews'][0]['payload'])
        review['resolved_execution_id'] = aliases[review['resolved_execution_id']]
        events = [*history['events'], *fixed_history['events']]
        event_excerpt = [dict(execution_id=aliases[e['execution_id']], event=e['event'],
                              **{key:e['payload'][key] for key in ('rule_id','decision','reason_code','classification') if key in e['payload']})
                         for e in sorted(events,key=lambda e:e['seq'])]
        excerpt_data = dict(failed=excerpt(failed), corrected=excerpt(passed), review=review, events=event_excerpt)
        diagnostics = copy.deepcopy(history['diagnostics'])
        with store._lock,store._conn:
            store._conn.execute("UPDATE policy_diagnostics SET expires_at='2000-01-01' WHERE deployment_id=?",(failed_id,))
        expired = life.details(store,failed_id)
        assert expired['diagnostics'][0]['payload'] is None
        assert expired['failure_reviews'] == history['failure_reviews']
        assert read(store, failed_id)['results'][0] == failed
        return excerpt_data, diagnostics, read(store,fixed_id)['summaries']
    finally:
        store.close()


class VersionContractTests(unittest.TestCase):
    def test_source_evidence_reasons_and_links(self):
        blank = next(i for i,line in enumerate((SOURCE/'src/server.js').read_text().splitlines(),1) if not line.strip())
        cases = [('src/missing.js:1','non_source_evidence'), ('package-lock.json:1','non_source_evidence'),
                 ('src/server.js:999999','invalid_source_evidence'), (f'src/server.js:{blank}','invalid_source_evidence')]
        for citation,code in cases:
            value=copy.deepcopy(INTENT);value['workloads'][0]['evidence']=[citation]
            reports=[]
            with self.subTest(citation=citation), observe(reports.append):
                with self.assertRaisesRegex(SourcePolicyError,'^'+code+'$'):
                    validate_demo_intent(value,SOURCE,MAPPING)
                self.assertEqual(reports[-1]['rules'][0]['reason_code'],code)
                self.assertIn(code,(DOCS/'troubleshooting.md').read_text())
                self.assertIn('{#'+code.replace('_','-')+'}',(DOCS/'rules/G-002.md').read_text())

    def test_plan_example_passes_profile_and_relationship(self):
        value=example('rules/L-001.md')
        self.assertEqual(value,PLAN)
        validate_demo_plan(value,MAPPING)
        validate_plan(INTENT,value)

    def test_plan_contract_boundaries(self):
        minimal={k:v for k,v in PLAN.items() if k in {'source_revision','target','app','image_tag','services','logs','mermaid'}}
        parsed=Plan.model_validate(minimal)
        self.assertEqual((parsed.schema_version,parsed.db,parsed.storage,parsed.secrets,parsed.config,parsed.est_monthly_krw),('1.0.0',None,None,[],{},None))
        for value in ({k:v for k,v in PLAN.items() if k!='mermaid'}, PLAN|{'extra':1}, PLAN|{'image_tag':'app:wrong'},
                      PLAN|{'services':[]}, PLAN|{'secrets':[]}, PLAN|{'logs':'cloudwatch'}, PLAN|{'config':{}}, PLAN|{'est_monthly_krw':-1}):
            with self.subTest(value=value),self.assertRaises(ValidationError):Plan.model_validate(value)
        http=PLAN['services'][0]
        worker=dict(name='worker',kind='worker',cpu=256,mem=512,command='node src/worker.js')
        Plan.model_validate(PLAN|{'services':[http,worker]})
        # Schema accepts these; source profile is the separate narrower boundary.
        Plan.model_validate(PLAN|{'services':[http|{'port':65535,'command':'node other.js'}]})
        for change in ({'public':True},{'port':3000},{'health':'/health'},{'command':''}):
            with self.subTest(change=change),self.assertRaises(ValidationError):
                Plan.model_validate(PLAN|{'services':[http,worker|change]})

    def test_manifest_example_is_real_generator_output(self):
        with tempfile.TemporaryDirectory() as folder:
            bundle=Path(folder).resolve()/'bundle'
            actual=patch_snapshot(SOURCE,MAPPING,PLAN,bundle)
            documented=example('rules/P-001.md')
            self.assertEqual(documented,actual)
            (bundle/'manifest.json').write_text(json.dumps(documented))
            report=[]
            with observe(report.append):validate_patch(SOURCE,bundle,PLAN)
            self.assertEqual(report[-1]['decision'],'PASS')
            self.assertTrue(report[-1]['complete'])

    def test_manifest_comparisons_and_metadata_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            bundle=Path(folder).resolve()/'bundle'
            actual=patch_snapshot(SOURCE,MAPPING,PLAN,bundle)
            for field,code in [('schema_version','manifest_version_unsupported'),('target','revision_or_target_mismatch'),
                               ('requires_policy_gate','manifest_gate_flag_missing'),('original_digest','artifact_digest_mismatch'),
                               ('changes','change_manifest_mismatch'),('status','patch_status_mismatch')]:
                value={k:v for k,v in actual.items() if k!=field}
                (bundle/'manifest.json').write_text(json.dumps(value))
                with self.subTest(field=field),self.assertRaisesRegex(PolicyError,'^'+code+'$'):
                    validate_patch(SOURCE,bundle,PLAN)
            for changes in (list(reversed(actual['changes'])),[item|{'extra':True} for item in actual['changes']]):
                (bundle/'manifest.json').write_text(json.dumps(actual|{'changes':changes}))
                with self.assertRaisesRegex(PolicyError,'^change_manifest_mismatch$'):validate_patch(SOURCE,bundle,PLAN)
            value={k:v for k,v in actual.items() if k not in {'profile','excluded_paths','data_migration_performed'}}
            (bundle/'manifest.json').write_text(json.dumps(value|{'annotation':'not an approval'}))
            validate_patch(SOURCE,bundle,PLAN)

    def test_record_walkthrough(self):
        with tempfile.TemporaryDirectory() as folder:
            actual,diagnostics,summaries=record_walkthrough(Path(folder))
        self.assertEqual(example('logs.md'),actual)
        self.assertEqual(diagnostics[0]['payload'],dict(text='non_source_evidence',masked=False,truncated=False))
        self.assertEqual(summaries[0]['decision'],'NOT_RUN')
        self.assertFalse(summaries[0]['complete'])
        self.assertEqual(actual['failed']['decision'],'BLOCK')
        # Completed source inspection still blocked; completion is not permission.
        self.assertTrue(actual['failed']['complete'])
        self.assertEqual(actual['corrected']['decision'],'PASS')
        self.assertEqual(actual['review']['fix_commit'],'')
        self.assertFalse(actual['review']['identity_verified'])
