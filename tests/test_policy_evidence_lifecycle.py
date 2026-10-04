"""Historical evidence updates remain advisory and cannot mutate deployment inputs."""
import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from schemas import Intent, Plan, RepoMap
from code_patch import patch_snapshot
from control_plane.app import create_app
from control_plane.db import Status
from control_plane import policy_lifecycle as life
from policy_gate.catalog import compare, identity, release

ROOT = Path(__file__).resolve().parents[1]


class EvidenceLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.app = create_app(db_path=self.root / 'state.sqlite')
        self.store = self.app.state.store
        self.addCleanup(self.store.close)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        project = self.client.post('/api/projects', json={
            'repo_url': 'https://github.com/Team-InfraMorph/demo-app', 'targets': ['local']}).json()
        self.did = self.store.begin_deploy(project['project_id'])

    def old_binding(self, version='1.1.0'):
        old = copy.deepcopy(release(version))
        if version == '1.1.0':
            for rule in old['rules']:
                if rule['id'] in {'I-002', 'I-003'}:
                    rule['revision'] = 1
            old['changes'] = [c for c in old['changes'] if c['id'] == 'bounded-policy-repair']
        meta = identity() | {'version': version, 'policy_digest': 'b' * 64}
        self.store._conn.execute('INSERT OR REPLACE INTO policy_releases VALUES (?,?,?)',
                                 ('b' * 64, json.dumps({'identity': meta, 'release': old}), life.now()))
        self.store._conn.execute('INSERT OR REPLACE INTO policy_bindings VALUES (?,?)', (self.did, 'b' * 64))
        return old

    def preserve(self, version='v1', *, weak_db=False):
        source = ROOT / 'tests/fixtures/analyzer' / version / 'snapshot'
        mapping = RepoMap.model_validate_json((source.parent / 'repo_map.json').read_text())
        intent = Intent.model_validate_json((ROOT / 'schemas/fixtures' / version / 'intent.json').read_text())
        plan = Plan.model_validate_json((ROOT / 'schemas/fixtures' / version / 'plan.local.json').read_text())
        if weak_db:
            intent.state[0].evidence = ['prisma/schema.prisma:7']
        bundle = self.root / 'bundle'
        manifest = patch_snapshot(source, mapping, plan, bundle)
        value = dict(snapshot=str(source), bundle=str(bundle), repo_map=mapping.model_dump(mode='json'),
                     intent=intent.model_dump(mode='json'), plan=plan.model_dump(mode='json'),
                     artifact=dict(source_revision=mapping.commit, target='local', image='app:' + mapping.commit,
                                   platform='linux/amd64'), image_id='sha256:' + 'a' * 64,
                     **{k: manifest[k] for k in ('original_digest', 'patched_digest', 'diff_sha256')})
        life.receipt(self.store, self.did, 'local', value)
        return value

    def live(self):
        self.store.set_target_status(self.did, 'local', Status.LIVE)
        self.store.set_status(self.did, Status.LIVE)

    def test_version_compare_describes_condition_and_action_changes(self):
        result = compare('1.0.0', '1.1.0')
        self.assertTrue({'I-002', 'I-003'} <= set(result['changed']))
        evidence = [c for c in result['changes'] if c.get('historical_mode') == 'advisory']
        self.assertEqual(len(evidence), 2)
        self.assertTrue(all(c['before_condition'] and c['after_condition'] and c['actions'] for c in evidence))
        self.assertTrue(any(c['id'] == 'bounded-policy-repair' for c in result['changes']))

    def test_same_version_comparison_uses_stored_rule_revisions(self):
        self.old_binding()
        response = self.client.get('/api/policies/1.1.0/compare', params={
            'base': '1.1.0', 'base_policy_digest': 'b' * 64})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result['comparison_kind'], 'implementation')
        self.assertEqual(result['history_status'], 'known')
        self.assertEqual(set(result['changed']), {'I-002', 'I-003'})
        self.assertEqual(len(result['changes']), 2)

    def test_unknown_digest_and_implementation_only_changes_need_confirmation(self):
        response = self.client.get('/api/policies/1.1.0/compare', params={
            'base': '1.1.0', 'base_policy_digest': 'e' * 64})
        self.assertEqual(response.json()['history_status'], 'confirmation_required')
        self.assertEqual(response.json()['changes'], [])
        unchanged = copy.deepcopy(release())
        self.assertEqual(compare('1.1.0', '1.1.0', base_release=unchanged)['history_status'], 'confirmation_required')

    def test_v1_does_not_recommend_worker_recheck(self):
        self.preserve('v1'); self.old_binding(); self.live()
        result = life.transition(self.store, self.did, 'local')
        self.assertTrue(result['recheck'])
        self.assertEqual([c['impact_scope'] for c in result['changes']], ['db'])
        worker = next(c for c in result['rule_impacts'] if c['impact_scope'] == 'worker')
        self.assertEqual(worker['applicability'], 'not_applicable')
        self.assertTrue(result['advisory'])
        self.assertFalse(result['redeploy'])

    def test_missing_receipt_is_unknown_not_unaffected(self):
        self.old_binding(); self.live()
        result = life.transition(self.store, self.did, 'local')
        self.assertEqual(result['applicability'], 'unknown')
        self.assertTrue(all(c['applicability'] == 'unknown' for c in result['changes']))
        life.schedule(self.store); life.run_next(self.store)
        self.assertEqual(life.impacts(self.store)[0]['action'], 'unavailable')
        self.assertEqual(self.store.get_deployment(self.did)['status'], 'LIVE')

    def test_worker_presence_is_verified_from_source_even_if_claim_omits_it(self):
        value = self.preserve('v2')
        # Simulate an older preserved claim: raw source still includes the worker.
        value['intent']['workloads'] = [w for w in value['intent']['workloads'] if w['kind'] != 'worker']
        self.store._conn.execute('DELETE FROM policy_receipts WHERE deployment_id=?', (self.did,))
        life.receipt(self.store, self.did, 'local', value)
        self.assertTrue(life.preserved_scope(self.store, self.did, 'local')['worker'])

    def test_read_only_recheck_preserves_receipt_and_raw_block(self):
        self.preserve(weak_db=True); self.old_binding(); self.live()
        before = self.store._one('SELECT payload FROM policy_receipts WHERE deployment_id=?', (self.did,))[0]
        with patch('builder.runtime.build', side_effect=AssertionError('must not build')):
            life.schedule(self.store)
            life.run_next(self.store)
        item = life.impacts(self.store)[0]
        self.assertEqual(item['latest_recheck']['decision'], 'BLOCK')
        self.assertEqual(item['latest_recheck']['reason_code'], 'db_provider_evidence_missing')
        self.assertEqual(item['action'], 'evidence_confirmation_required')
        self.assertEqual(item['status'], 'LIVE')
        self.assertEqual(self.store._one('SELECT payload FROM policy_receipts WHERE deployment_id=?', (self.did,))[0], before)
        with self.assertRaisesRegex(ValueError, 'stale_or_failed_policy_review'):
            life.review(self.store, self.did, item['job_id'], identity()['policy_digest'])

    def test_examples_are_read_only_and_excluded_from_operational_records(self):
        item = dict(id='policy-example-db', project_id='example', target='local',
                    policy={'family': 'inframorph-policy', 'version': '1.0.0'}, active=identity(),
                    impact={'changes': []}, action='evidence_confirmation_required', reason_codes=['db_provider_evidence_missing'])
        artifact = self.root / 'examples.json'
        artifact.write_text(json.dumps({'schema_version': 1, 'items': [item]}))
        self.app.state.policy_examples_path = artifact
        data = self.client.get('/api/policies/impacts?dataset=examples').json()
        self.assertTrue(data['read_only'])
        self.assertTrue(data['items'][0]['read_only'])
        self.assertFalse(data['items'][0]['service_verified'])
        self.assertEqual(self.client.get('/api/policies/impacts').json()['items'], [])
        self.assertEqual(self.client.get('/api/policies/statistics').json()['items'], [])
        for suffix, body in [('policy-rechecks', {'target': 'local'}),
                             ('policy-reviews', {'job_id': 'example', 'policy_digest': identity()['policy_digest']}),
                             ('policy-redeploy', {'commit': 'a' * 40, 'policy_digest': identity()['policy_digest']})]:
            result = self.client.post('/api/deployments/policy-example-db/' + suffix, json=body)
            self.assertEqual(result.status_code, 409, result.text)
            self.assertEqual(result.json()['detail'], 'policy_example_read_only')
        self.assertEqual(self.store._all('SELECT * FROM policy_jobs', ()), [])

    def test_examples_artifact_failure_is_not_an_empty_success(self):
        self.app.state.policy_examples_path = self.root / 'missing.json'
        result = self.client.get('/api/policies/impacts?dataset=examples')
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json()['detail'], 'policy_examples_unavailable')

    def test_original_decision_requires_all_original_stages(self):
        stored = {'rules': [{'stage': 'intent'}, {'stage': 'patch'}]}
        rows = [{'family': 'inframorph-policy', 'policy_digest': 'old',
                 'stage': 'intent', 'decision': 'PASS', 'complete': True}]
        self.assertEqual(life.original_decision(rows, stored), 'NOT_RUN')
        rows.append(rows[0] | {'stage': 'patch'})
        self.assertEqual(life.original_decision(rows, stored), 'PASS')
        rows.append(rows[0] | {'stage': 'patch', 'decision': 'BLOCK', 'complete': False})
        self.assertEqual(life.original_decision(rows, stored), 'BLOCK')
        self.assertIsNone(life.original_decision(rows, None))

    def test_failure_resolution_links_successor_but_rejects_wrong_target_old_or_weaker_result(self):
        from policy_gate.reporting import observe
        from policy_gate.gate import validate_intent, PolicyError
        from control_plane.policy_results import save
        source = ROOT / 'tests/fixtures/analyzer/v1/snapshot'
        good = json.loads((ROOT / 'schemas/fixtures/v1/intent.json').read_text())
        def report(value):
            rows = []
            with observe(rows.append):
                try:
                    validate_intent(value, source, value['source_revision'])
                except PolicyError:
                    pass
            return rows[-1]
        old_pass = report(good)
        save(self.store, self.did, 'local', 0, old_pass)
        bad = copy.deepcopy(good)
        bad['state'][0]['evidence'] = ['prisma/schema.prisma:7']
        failed = report(bad)
        save(self.store, self.did, 'local', 0, failed)
        body = dict(classification='violation', resolved_execution_id=old_pass['execution_id'])
        with self.assertRaisesRegex(ValueError, 'resolved_execution_not_passed'):
            life.failure_review(self.store, self.did, failed['execution_id'], body)
        project = self.store.get_deployment(self.did)['project_id']
        self.store.set_status(self.did, Status.FAILED)
        successor = self.store.begin_deploy(project)
        for target, weaker in [('aws', False), ('local', True)]:
            passed = report(good)
            if weaker:
                for rule in passed['rules']:
                    if rule['rule_id'] == 'I-003':
                        rule['revision'] = 1
            save(self.store, successor, target, 0, passed)
            with self.assertRaisesRegex(ValueError, 'resolved_execution_not_passed'):
                life.failure_review(self.store, self.did, failed['execution_id'],
                                    body | {'resolved_execution_id': passed['execution_id']})
        passed = report(good)
        save(self.store, successor, 'local', 0, passed)
        life.failure_review(self.store, self.did, failed['execution_id'],
                            body | {'resolved_execution_id': passed['execution_id']})
        review = life.details(self.store, self.did)['failure_reviews'][-1]['payload']
        self.assertEqual(review['resolved_deployment_id'], successor)
        self.assertEqual(review['resolved_target'], 'local')
        self.assertEqual(review['resolved_source_revision'], good['source_revision'])
        self.assertEqual(review['resolved_policy_digest'], passed['policy_digest'])
        self.assertEqual(json.loads(self.store._one('SELECT payload FROM policy_results WHERE deployment_id=? AND seq=?',
                                                   (self.did, 2))[0])['decision'], 'BLOCK')

    def test_previous_policy_assets_are_unchanged(self):
        result = subprocess.run(['git', 'diff', '--exit-code', 'origin/main', '--',
                                 'policy_gate/baselines/1.0.0.json', 'policy_gate/releases/1.0.0.json',
                                 'policy_gate/docs/1.0.0'], cwd=ROOT, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout.decode())


if __name__ == '__main__':
    unittest.main()
