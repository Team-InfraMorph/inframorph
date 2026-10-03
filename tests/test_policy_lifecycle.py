"""Policy 1.0.0 contracts, durable evidence and non-destructive updates."""
import copy
import json
import tempfile
import unittest
from datetime import datetime,timezone,timedelta
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from schemas import Intent,Plan,RepoMap
from policy_gate.catalog import identity,release,compare,fingerprint,index
from policy_gate.reporting import checked,observe,rule
from policy_gate.gate import PolicyError,validate_intent,validate_plan,validate_patch
from code_patch import patch_snapshot
from control_plane.app import create_app
from control_plane.db import Status,Store
from control_plane.policy_results import save,read,check
from control_plane import policy_lifecycle as life

ROOT=Path(__file__).resolve().parents[1]


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        self.app=create_app(db_path=self.root/'state.sqlite')
        self.store=self.app.state.store;self.addCleanup(self.store.close)
        self.client=TestClient(self.app);self.addCleanup(self.client.close)
        p=self.client.post('/api/projects',json={'repo_url':'https://github.com/Team-InfraMorph/demo-app','targets':['local']}).json()
        self.project=p['project_id'];self.did=self.store.begin_deploy(self.project)
        self.source=ROOT/'tests/fixtures/analyzer/v1/snapshot'
        self.mapping=RepoMap.model_validate_json((ROOT/'tests/fixtures/analyzer/v1/repo_map.json').read_text())
        self.intent=Intent.model_validate_json((ROOT/'schemas/fixtures/v1/intent.json').read_text())
        self.plan=Plan.model_validate_json((ROOT/'schemas/fixtures/v1/plan.local.json').read_text())

    def report(self,invalid=False):
        rows=[];value=self.intent.model_dump(mode='json')
        if invalid:value['state'][0]['engine']='postgresql'
        with observe(rows.append):
            try:validate_intent(value,self.source,self.mapping.commit)
            except PolicyError:
                if not invalid:raise
        return rows[-1]

    def live(self):
        self.store.set_target_status(self.did,'local',Status.LIVE)
        self.store.set_status(self.did,Status.LIVE)

    def receipt(self):
        bundle=self.root/'bundle'
        manifest=patch_snapshot(self.source,self.mapping,self.plan,bundle)
        value=dict(snapshot=str(self.source),bundle=str(bundle),repo_map=self.mapping.model_dump(mode='json'),
            intent=self.intent.model_dump(mode='json'),plan=self.plan.model_dump(mode='json'),
            artifact=dict(source_revision=self.mapping.commit,target='local',image='app:'+self.mapping.commit,platform='linux/amd64'),
            image_id='sha256:'+'a'*64,**{k:manifest[k] for k in ('original_digest','patched_digest','diff_sha256')})
        life.receipt(self.store,self.did,'local',value);return value

    def test_main_onprem_scope_is_recorded_without_fabricating_complete_inspection(self):
        project=self.store.create_project('https://github.com/Team-InfraMorph/demo-app','main',['local','onprem'])
        did=self.store.begin_deploy(project['project_id'])
        save(self.store,did,'onprem',0,self.report())
        summary=read(self.store,did)['summaries'][0]
        self.assertFalse(summary['complete'])
        self.assertEqual(summary['decision'],'NOT_RUN')
        self.store.set_target_status(did,'onprem',Status.LIVE)
        change=life.transition(self.store,did,'onprem')
        self.assertTrue(change['review'])
        self.assertEqual(change['reason'],'target_scope_requires_review')
        response=self.client.post(f'/api/deployments/{did}/policy-rechecks',json={'target':'onprem'})
        self.assertEqual(response.status_code,202,response.text)
        job=life.details(self.store,did)['jobs'][-1]
        self.assertEqual(job['result']['decision'],'UNAVAILABLE')
        self.assertEqual(self.store.get_deployment(did)['targets']['onprem']['status'],'LIVE')

    def test_actual_rules_have_evidence_and_no_missing_success(self):
        report=self.report()
        self.assertEqual(report['family'],'inframorph-policy');self.assertEqual(report['version'],'1.0.0')
        self.assertEqual(report['decision'],'PASS');self.assertTrue(report['complete'])
        self.assertEqual(set(report['required_rules']),set(report['evaluated_rules']))
        db=next(r for r in report['rules'] if r['rule_id']=='I-003')
        self.assertEqual(db['evidence']['observed'],'sqlite')
        self.assertTrue(all(r.get('finished_at') for r in report['rules']))

    def test_early_block_preserves_unexecuted_rules(self):
        report=self.report(True)
        self.assertEqual(report['decision'],'BLOCK');self.assertFalse(report['complete'])
        self.assertEqual(next(r for r in report['rules'] if r['rule_id']=='I-004')['decision'],'NOT_RUN')
        self.assertEqual(next(r for r in report['rules'] if r['rule_id']=='I-003')['evidence']['observed'],'sqlite')

    def test_missing_rule_cannot_return_pass(self):
        @checked('plan')
        def broken():
            with rule('L-001'):pass
        reports=[]
        with observe(reports.append),self.assertRaisesRegex(PolicyError,'policy_rules_incomplete'):broken()
        self.assertEqual(reports[-1]['decision'],'ERROR')

    def test_duplicate_delivery_idempotent_but_rerun_preserved(self):
        first=self.report();second=self.report()
        save(self.store,self.did,'local',0,first);save(self.store,self.did,'local',0,first);save(self.store,self.did,'local',0,second)
        self.assertEqual(len(read(self.store,self.did)['results']),2)
        altered=first|{'title':'changed'}
        with self.assertRaisesRegex(ValueError,'policy_execution_conflict'):save(self.store,self.did,'local',0,altered)

    def test_forged_complete_ledger_rejected(self):
        report=self.report();report['evaluated_rules']=[]
        with self.assertRaisesRegex(ValueError,'policy_rules_incomplete'):save(self.store,self.did,'local',0,report)

    def test_decision_write_failure_prevents_next_step(self):
        after=[]
        with patch('control_plane.policy_results.save',side_effect=OSError('disk')):
            with self.assertRaises(OSError):
                check(self.store,self.did,'local','intent',lambda:validate_intent(self.intent,self.source,self.mapping.commit))
                after.append('build')
        self.assertEqual(after,[])

    def test_policy_change_rejects_old_binding(self):
        life.guard(self.store,self.did)
        other=identity()|{'policy_digest':'0'*64}
        with patch('control_plane.policy_lifecycle.identity',return_value=other):
            with self.assertRaisesRegex(ValueError,'policy_runtime_changed'):life.guard(self.store,self.did)

    def test_new_and_legacy_histories_are_not_overwritten(self):
        old={'version':'2.2.0','decision':'PASS','stage':'intent','complete':True}
        save(self.store,self.did,'local',0,old);save(self.store,self.did,'local',0,self.report())
        self.assertEqual(read(self.store,self.did)['results'][0]['version'],'2.2.0')
        other=Store(self.root/'state.sqlite');self.addCleanup(other.close)
        self.assertEqual(len(read(other,self.did)['results']),2)

    def test_redaction_limits_and_expiration_keep_audit(self):
        execution=self.report(True);save(self.store,self.did,'local',0,execution)
        ident=life.diagnostic(self.store,self.did,execution['execution_id'],'Authorization: Bearer supersecret\npassword=othersecret\npostgresql://u:p@host/db\n'+('a'*9000))
        row=self.store._one('SELECT payload FROM policy_diagnostics WHERE id=?',(ident,));data=json.loads(row[0])
        self.assertNotIn('supersecret',data['text']);self.assertNotIn('othersecret',data['text']);self.assertNotIn('u:p',data['text'])
        self.assertTrue(data['truncated']);self.assertLessEqual(len(data['text'].encode()),8192)
        with self.store._lock,self.store._conn:self.store._conn.execute("UPDATE policy_diagnostics SET expires_at='2000-01-01' WHERE id=?",(ident,))
        life.expire_logs(self.store)
        self.assertIsNone(self.store._one('SELECT payload FROM policy_diagnostics WHERE id=?',(ident,))[0])
        self.assertTrue(life.details(self.store,self.did)['events']);self.assertEqual(read(self.store,self.did)['results'][0]['decision'],'BLOCK')

    def test_missing_receipt_recheck_is_unavailable_and_no_deploy(self):
        self.live();job=life.enqueue(self.store,self.did,'local');life.run_next(self.store)
        result=json.loads(self.store._one('SELECT result FROM policy_jobs WHERE id=?',(job,))[0])
        self.assertEqual(result['decision'],'UNAVAILABLE');self.assertEqual(self.store.get_deployment(self.did)['status'],'LIVE')
        with self.assertRaises(ValueError):life.review(self.store,self.did,job,identity()['policy_digest'])

    def test_recheck_runs_preserved_gates_and_review_is_bound(self):
        self.receipt();self.live()
        with patch('builder.runtime.build',side_effect=AssertionError('no build')):
            job=life.enqueue(self.store,self.did,'local');life.run_next(self.store)
        result=json.loads(self.store._one('SELECT result FROM policy_jobs WHERE id=?',(job,))[0])
        self.assertEqual(result['decision'],'PASS');self.assertGreaterEqual(len(result['executions']),6)
        self.assertFalse(result['live_drift_checked'])
        life.review(self.store,self.did,job,identity()['policy_digest'])
        with self.assertRaises(ValueError):life.review(self.store,self.did,job,'0'*64)

    def test_changed_snapshot_cannot_be_rechecked_green(self):
        value=self.receipt();self.live()
        # Simulate missing preserved input without modifying repository fixtures.
        with self.store._lock,self.store._conn:
            altered=value|{'snapshot':str(self.root/'missing')}
            self.store._conn.execute('UPDATE policy_receipts SET payload=?,digest=?',(json.dumps(altered),fingerprint(altered)))
        job=life.enqueue(self.store,self.did,'local');life.run_next(self.store)
        self.assertEqual(json.loads(self.store._one('SELECT result FROM policy_jobs WHERE id=?',(job,))[0])['decision'],'UNAVAILABLE')

    def test_queue_is_deduplicated_and_manual_rerun_kept(self):
        self.live();a=life.enqueue(self.store,self.did,'local',automatic=True)
        self.assertEqual(a,life.enqueue(self.store,self.did,'local',automatic=True));life.run_next(self.store)
        self.assertEqual(a,life.enqueue(self.store,self.did,'local',automatic=True))
        self.assertNotEqual(a,life.enqueue(self.store,self.did,'local'))

    def test_failure_classification_does_not_edit_verdict(self):
        report=self.report(True);save(self.store,self.did,'local',0,report)
        life.failure_review(self.store,self.did,report['execution_id'],{'classification':'unknown','summary':'소스 재확인 필요'})
        self.assertEqual(read(self.store,self.did)['results'][0]['decision'],'BLOCK')
        with self.assertRaises(ValueError):life.failure_review(self.store,self.did,report['execution_id'],{'classification':'false_positive'})

    def test_documents_are_versioned_and_unknown_version_is_not_latest(self):
        docs=index('1.0.0');self.assertGreater(len(docs),18)
        response=self.client.get('/api/policies').json()
        self.assertEqual(response['active']['version'],'1.0.0');self.assertEqual(response['legacy']['family'],'legacy')
        self.assertNotEqual(self.client.get('/api/policies/9.9.9').status_code,200)
        self.assertEqual(self.client.get('/api/policies/1.0.0/compare?base=legacy').json()['major'],True)
        self.assertEqual(self.client.get('/api/policies/1.0.0/compare?base=1.0.0').json()['changes'],[])

    def test_history_gap_is_not_no_impact(self):
        with self.assertRaisesRegex(ValueError,'policy_history_incomplete'):compare('0.9.0','1.0.0')

    def test_compare_accumulates_intermediate_patch_requirement(self):
        baseline=release();mid=copy.deepcopy(baseline);latest=copy.deepcopy(baseline)
        mid.update(version='1.0.1',previous='1.0.0',changes=[{'id':'fix','recheck':True}])
        latest.update(version='1.1.0',previous='1.0.1',changes=[{'id':'docs','recheck':False}])
        with patch('policy_gate.catalog.release',side_effect=lambda v:{'1.0.0':baseline,'1.0.1':mid,'1.1.0':latest}[v]):
            delta=compare('1.0.0','1.1.0')
        self.assertEqual([c['id'] for c in delta['changes']],['fix','docs']);self.assertFalse(delta['major'])

    def test_target_specific_impacts_and_legacy_transition(self):
        self.live();items=life.impacts(self.store)
        self.assertEqual(len(items),1);self.assertEqual(items[0]['policy']['family'],'unknown')
        self.assertTrue(items[0]['impact']['review'])
        self.assertEqual(items[0]['target'],'local')

    def test_api_rejects_unknown_targets_and_failed_review(self):
        self.live()
        self.assertEqual(self.client.post(f'/api/deployments/{self.did}/policy-rechecks',json={'target':'other'}).status_code,422)
        self.assertEqual(self.client.post(f'/api/deployments/{self.did}/policy-reviews',json={'job_id':'missing','policy_digest':'0'*64}).status_code,409)
        self.assertEqual(self.client.post(f'/api/deployments/{self.did}/policy-redeploy',json={'commit':'a'*40,'policy_digest':identity()['policy_digest']}).status_code,409)

    def test_start_event_survives_interruption_and_restart(self):
        from control_plane.policy_results import start
        report=self.report();start(self.store,self.did,'local',report)
        with TestClient(self.app):pass
        events=life.details(self.store,self.did)['events']
        self.assertTrue(any(e['event']=='inspection_interrupted' for e in events))
        self.assertEqual(read(self.store,self.did)['results'],[])

    def test_diagnostic_write_failure_does_not_change_decision(self):
        with self.store._lock,self.store._conn:
            self.store._conn.execute("CREATE TRIGGER reject_diagnostic BEFORE INSERT ON policy_diagnostics BEGIN SELECT RAISE(FAIL,'unavailable'); END;")
        report=self.report(True);save(self.store,self.did,'local',0,report)
        self.assertEqual(read(self.store,self.did)['results'][0]['decision'],'BLOCK')
        self.assertTrue(any(e['event']=='diagnostic_unavailable' for e in life.details(self.store,self.did)['events']))

    def test_failure_review_is_masked_and_stats_use_actual_execution(self):
        report=self.report(True);save(self.store,self.did,'local',0,report)
        life.failure_review(self.store,self.did,report['execution_id'],dict(classification='unknown',summary='token=sensitive password=sensitive'))
        self.assertNotIn('sensitive',json.dumps(life.details(self.store,self.did)['failure_reviews']))
        stats=life.statistics(self.store)
        db=next(r for r in stats if r['rule_id']=='I-003')
        self.assertEqual((db['performed'],db['blocked'],db['false_positive'],db['unresolved']),(1,1,0,1))
        missed=next(r for r in stats if r['rule_id']=='I-004')
        self.assertEqual((missed['performed'],missed['not_run']),(0,1))

    def test_resolution_can_link_a_successor_but_not_an_unrelated_rule(self):
        failed=self.report(True);save(self.store,self.did,'local',0,failed)
        self.store.set_status(self.did,Status.FAILED)
        successor=self.store.begin_deploy(self.project);passed=self.report();save(self.store,successor,'local',0,passed)
        ident=life.failure_review(self.store,self.did,failed['execution_id'],dict(classification='violation',resolved_execution_id=passed['execution_id']))
        self.assertTrue(ident)
        wrong=copy.deepcopy(passed);wrong['stage']='patch'
        with self.store._lock,self.store._conn:
            self.store._conn.execute('UPDATE policy_results SET payload=? WHERE deployment_id=?',(json.dumps(wrong),successor))
        with self.assertRaisesRegex(ValueError,'resolved_execution_not_passed'):
            life.failure_review(self.store,self.did,failed['execution_id'],dict(classification='violation',resolved_execution_id=passed['execution_id']))

    def test_large_log_and_forged_separator_are_not_events(self):
        before=len(life.details(self.store,self.did)['events'])
        life.diagnostic(self.store,self.did,None,'x'*70000)
        life.diagnostic(self.store,self.did,None,'<script>alert(1)</script>\x00\ninspection_finished PASS')
        data=life.details(self.store,self.did)
        self.assertEqual(len(data['events']),before)
        self.assertEqual(data['diagnostics'][0]['payload']['text'],'diagnostic_input_limit')
        self.assertNotIn('\x00',data['diagnostics'][1]['payload']['text'])

    def test_old_document_revision_and_unknown_revision(self):
        result=self.client.get('/api/policies/1.0.0?revision=1')
        self.assertEqual(result.status_code,200)
        self.assertTrue(all(d['revision']==1 and d['source'] and d['section_ids'] for d in result.json()['documents']))
        self.assertEqual(self.client.get('/api/policies/1.0.0?revision=99').status_code,409)

    def test_patch_change_can_require_recheck_without_major(self):
        oldmeta=identity()|{'version':'1.0.0','policy_digest':'b'*64}
        self.store._conn.execute('INSERT INTO policy_releases VALUES (?,?,?)',('b'*64,json.dumps({'identity':oldmeta}), '2026-01-01'))
        self.store._conn.execute('INSERT INTO policy_bindings VALUES (?,?)',(self.did,'b'*64))
        delta=dict(major=False,changes=[dict(recheck=True,review=False,redeploy=False,targets=['local'],profiles=['reviewed-node22'])])
        with patch('control_plane.policy_lifecycle.identity',return_value=oldmeta|{'version':'1.0.1','policy_digest':'c'*64}),patch('control_plane.policy_lifecycle.compare',return_value=delta):
            self.assertTrue(life.transition(self.store,self.did,'local')['recheck'])
            self.assertFalse(life.transition(self.store,self.did,'aws')['recheck'])

    def test_changed_receipt_invalidates_review(self):
        self.receipt();self.live();job=life.enqueue(self.store,self.did,'local');life.run_next(self.store)
        with self.store._lock,self.store._conn:self.store._conn.execute("UPDATE policy_receipts SET digest=? WHERE deployment_id=?",('0'*64,self.did))
        with self.assertRaises(ValueError):life.review(self.store,self.did,job,identity()['policy_digest'])

    def test_started_and_observed_times_are_distinct_fields(self):
        report=self.report();save(self.store,self.did,'local',0,report)
        event=next(e for e in life.details(self.store,self.did)['events'] if e['event']=='inspection_started')
        self.assertEqual(event['occurred_at'],report['started_at'])
        self.assertGreaterEqual(event['observed_at'],event['occurred_at'])

    def test_missing_catalog_rule_cannot_forge_complete_pass(self):
        report=self.report();removed=report['rules'].pop()['rule_id']
        report['required_rules'].remove(removed);report['evaluated_rules'].remove(removed)
        with self.assertRaisesRegex(ValueError,'policy_rules_incomplete'):save(self.store,self.did,'local',0,report)

    def test_summary_is_not_pass_after_only_intent(self):
        save(self.store,self.did,'local',0,self.report())
        summary=read(self.store,self.did)['summaries'][0]
        self.assertEqual(summary['decision'],'NOT_RUN');self.assertIn('patch',summary['missing_stages'])

    def test_mutated_inputs_cannot_be_reviewed_after_pass(self):
        self.receipt();self.live();job=life.enqueue(self.store,self.did,'local');life.run_next(self.store)
        (self.root/'bundle'/'patch.diff').write_text('changed')
        with self.assertRaisesRegex(ValueError,'preserved_inputs_changed'):life.review(self.store,self.did,job,identity()['policy_digest'])

    def test_redeploy_request_cannot_change_policy_before_execution(self):
        with self.store._lock,self.store._conn:self.store._conn.execute('INSERT INTO policy_successors VALUES (?,?,?)',(self.did,self.did,'0'*64))
        with self.assertRaisesRegex(ValueError,'stale_policy_redeploy'):life.guard(self.store,self.did)

    def test_recheck_does_not_relabel_historical_policy(self):
        self.receipt();self.live()
        with self.store._lock,self.store._conn:self.store._conn.execute('DELETE FROM policy_bindings WHERE deployment_id=?',(self.did,))
        save(self.store,self.did,'local',0,dict(version='2.2.0',decision='PASS',stage='intent'))
        life.enqueue(self.store,self.did,'local');life.run_next(self.store)
        self.assertEqual(life.impacts(self.store)[0]['policy'],{'family':'legacy','version':'2.2.0'})

    def test_no_impact_is_persisted_once_without_inventing_pass(self):
        life.guard(self.store,self.did);self.live()
        life.schedule(self.store);life.schedule(self.store)
        data=life.details(self.store,self.did)
        self.assertEqual(len(data['impact_assessments']),1)
        self.assertFalse(data['impact_assessments'][0]['payload']['recheck'])
        self.assertEqual(read(self.store,self.did)['results'],[])
        self.assertEqual(data['jobs'],[])

if __name__=='__main__':unittest.main()
