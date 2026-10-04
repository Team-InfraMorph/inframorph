"""Reviewed board source, target boundaries and actual-execution evidence contracts."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from analyzer.source_policy import board_profiles, validate_demo_intent, validate_demo_plan, SourcePolicyError
from adapters.local.board_check import note_event, check_worker, check_assets
from builder.runtime import RuntimeFailure
from control_plane.b_bridge import GitHubModules, DemoModules
from policy_gate.catalog import compare, release
from schemas import RepoMap, Intent

ROOT = Path(__file__).resolve().parents[1]


class BoardTests(unittest.TestCase):
    def test_versions_keep_fixture_mode_separate(self):
        self.assertEqual([p['id'] for p in DemoModules().versions()], ['v1','v2'])
        versions=GitHubModules().versions()
        self.assertEqual([p['id'] for p in versions], ['v1','v2','v3'])
        for p in versions[:2]:
            self.assertIn('gcp', p['supported_targets'])
        for p in versions[2:]:
            self.assertEqual(p['supported_targets'], ['local'])
            self.assertRegex(p['commit_sha'], r'^[0-9a-f]{40}$')

    def test_policy_history_is_additive(self):
        delta=compare('1.0.0','1.1.0')
        self.assertEqual(delta['changed'], ['G-002','G-003'])
        self.assertFalse(delta['major'])
        self.assertEqual({c['id'] for c in delta['changes']}, {'bounded-policy-repair', 'reviewed-demo-board'})
        board_change=next(c for c in delta['changes'] if c['id']=='reviewed-demo-board')
        self.assertFalse(board_change['recheck'] or board_change['redeploy'])
        self.assertTrue(next(c for c in delta['changes'] if c['id']=='bounded-policy-repair')['recheck'])
        self.assertEqual(len(release('1.0.0')['rules']),18)

    def test_v2_to_v3_preserves_infrastructure_structure(self):
        from planner.engine import make_plan
        from control_plane.change_detector import plan_diff
        old=json.loads((ROOT/'schemas/fixtures/v2/intent.json').read_text())
        new=json.loads((ROOT/'tests/fixtures/board/v3/intent.json').read_text())
        old_plan=make_plan(old,'local').model_dump(mode='json')
        new_plan=make_plan(new,'local').model_dump(mode='json')
        self.assertEqual(plan_diff({'local':old_plan},{'local':new_plan}),[])
        self.assertEqual({w['name'] for w in new['workloads']},{'web','worker'})

    def test_aws_and_unregistered_board_plan_rejected(self):
        for p in board_profiles():
            mapping=RepoMap.model_validate_json((ROOT/'tests/fixtures/analyzer/v1/repo_map.json').read_text())
            mapping=mapping.model_copy(update={'commit':p['commit_sha'],'tree':list(p['files'])})
            plan=json.loads((ROOT/'schemas/fixtures/v1/plan.aws.json').read_text())
            plan['source_revision']=mapping.commit;plan['image_tag']='app:'+mapping.commit
            with self.assertRaises(SourcePolicyError):validate_demo_plan(plan,mapping,target='aws')
            mapping=mapping.model_copy(update={'commit':'a'*40})
            with self.assertRaises(SourcePolicyError):validate_demo_plan(plan,mapping,target='local')

    def test_worker_event_rejects_old_forged_and_wrong_counts(self):
        now=datetime.now(timezone.utc)
        def event(**kw):return json.dumps(dict(event='note_count',count=4,at=now.isoformat())|kw)
        for raw in ['note_count 4',event(count=True),event(count=-1),event(count=3),event(at=(now-timedelta(seconds=3)).isoformat()),event(at=(now+timedelta(seconds=30)).isoformat()),'<script>'+event(),event(extra='fake')]:
            self.assertIsNone(note_event(raw,since=now,minimum=4))
        self.assertEqual(note_event(event(count=8),since=now,minimum=4)['count'],8)

    def test_worker_running_is_not_enough_and_old_logs_fail(self):
        note={'id':1,'text':'probe'}
        def request(url,**kw):return json.dumps(note if kw else [note]).encode()
        def execute(args,**kw):
            if 'ps' in args:return 'container'
            if 'inspect' in args:return json.dumps([{'Image':'image','State':{'Running':True,'StartedAt':'now'}}])
            return json.dumps({'event':'note_count','count':100,'at':'2000-01-01T00:00:00Z'})
        with self.assertRaisesRegex(RuntimeFailure,'board_worker_timeout'):
            check_worker(execute,[], 'http://127.0.0.1:1','image',request,timeout=.01)
        def stopped(args,**kw):
            if 'inspect' in args:return json.dumps([{'Image':'other','State':{'Running':True}}])
            return execute(args,**kw)
        with self.assertRaisesRegex(RuntimeFailure,'board_worker_not_running'):
            check_worker(stopped,[], 'http://127.0.0.1:1','image',request)

    def test_worker_current_event_and_probe(self):
        note={'id':5,'text':'probe'}
        def request(url,**kw):return json.dumps(note if kw else [{'id':4},note]).encode()
        def execute(args,**kw):
            if 'ps' in args:return 'container'
            if 'inspect' in args:return json.dumps([{'Image':'image','State':{'Running':True,'StartedAt':'now'}}])
            return json.dumps({'event':'note_count','count':3,'at':datetime.now(timezone.utc).isoformat()})
        result=check_worker(execute,[],'http://127.0.0.1:1','image',request)
        self.assertEqual(result['probe_note_id'],5)
        self.assertEqual(result['minimum_count'],2)

    def test_source_bundle_rejects_ui_mutation_missing_mixed_and_caption_evidence(self):
        # Sources are materialized from the pinned demo-app commit, never copied into Git.
        from code_patch.runner import patch_snapshot
        from policy_gate.gate import validate_intent, validate_patch
        from planner.engine import make_plan
        for profile in board_profiles():
            case=profile['id'];source=ROOT/'.local/demo-board-sources'/case/'snapshot'
            metadata=ROOT/'tests/fixtures/board'/case
            mapping=RepoMap.model_validate_json((metadata/'repo_map.json').read_text())
            intent=Intent.model_validate_json((metadata/'intent.json').read_text())
            validate_demo_intent(intent,source,mapping)
            validate_intent(intent,source,mapping.commit)
            files={n:(source/n).read_bytes() for n in mapping.tree}
            for bad in [files|{'src/web/index.html':b'changed'}, {n:b for n,b in files.items() if n!='src/web/assets/flow.json'},files|{'src/web/extra.js':b'1'}]:
                with patch('analyzer.source_policy.read_snapshot',return_value=(bad,None)):
                    with self.assertRaises(SourcePolicyError):validate_demo_intent(intent,source,mapping)
            bad=intent.model_copy(deep=True);bad.workloads[0].evidence=['src/web/index.html:1']
            with self.assertRaisesRegex(SourcePolicyError,'non_source_evidence'):validate_demo_intent(bad,source,mapping)

            plan=make_plan(intent.model_dump(mode="json"), 'local')
            validate_demo_plan(plan,mapping)
            with tempfile.TemporaryDirectory() as tmp:
                bundle=Path(tmp).resolve()/'bundle'
                patch_snapshot(source,mapping,plan,bundle)
                validate_patch(source,bundle,plan)
                for name in profile['files']:
                    if name.startswith('src/web/'):
                        self.assertEqual((bundle/'source'/name).read_bytes(),files[name])
                (bundle/'source/src/web/index.html').write_text('tampered')
                from policy_gate.gate import PolicyError
                with self.assertRaises(PolicyError):validate_patch(source,bundle,plan)

class BoardRequestTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from control_plane.app import create_app
        from control_plane.runtime import LocalRuntime
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        # Offline API boundary checks; this runtime never invokes analysis.
        self.runtime=LocalRuntime(root=self.root/'runtime',b_modules=GitHubModules(),replay='unused.json')
        self.app=create_app(db_path=self.root/'db.sqlite',runtime=self.runtime)
        self.addCleanup(self.app.state.store.close)
        self.client=TestClient(self.app);self.addCleanup(self.client.close)

    def test_invalid_version_target_repo_and_arbitrary_commit_create_no_jobs(self):
        body={'repo_url':'https://github.com/Team-InfraMorph/demo-app','branch':'feat/e-demo-board','demo_version':'v3','targets':['local']}
        for changed,expected in [({'targets':['local','aws']},409),({'targets':['local','onprem']},409),({'demo_version':'unregistered'},422),({'demo_version':'board-v1'},422),({'demo_version':'board-v2'},422),({'commit_sha':'a'*40},422),({'repo_url':'https://github.com/Team-InfraMorph/redteam-repo'},409)]:
            self.assertEqual(self.client.post('/api/deploy',json=body|changed).status_code,expected)
        self.assertEqual(self.app.state.store._all('SELECT * FROM deployments',()),[])

    def test_existing_project_targets_cannot_bypass_board_restriction(self):
        store=self.app.state.store
        project=store.create_project('https://github.com/Team-InfraMorph/demo-app','main',['local','aws'])
        before=store.list_deployments(project['project_id'])
        result=self.client.post(f"/api/projects/{project['project_id']}/deploy",json={'demo_version':'v3'})
        self.assertEqual(result.status_code,409)
        self.assertEqual(before,store.list_deployments(project['project_id']))

    def test_exact_revision_is_pinned_before_analysis(self):
        store=self.app.state.store
        project=store.create_project('https://github.com/Team-InfraMorph/demo-app','feat/e-demo-board',['local'])
        observed=[]
        def record(store,deployment):
            observed.append(deployment['commit_sha'])
            from control_plane.analysis import AnalysisFailed
            raise AnalysisFailed('test_stop_before_model')
        with patch.object(self.runtime,'analyze',side_effect=record):
            response=self.client.post(f"/api/projects/{project['project_id']}/deploy",json={'demo_version':'v3','targets':['local']})
        self.assertEqual(response.status_code,202)
        self.assertEqual(observed,[board_profiles()[0]['commit_sha']])

    def test_v1_to_v3_keeps_same_project_and_waits_for_worker_approval(self):
        from planner.engine import make_plan
        store=self.app.state.store
        project=store.create_project('https://github.com/Team-InfraMorph/demo-app','feat/e-demo-board',['local'])
        values=[]
        baseline=json.loads((ROOT/'schemas/fixtures/v1/intent.json').read_text())
        values.append(dict(commit_sha=baseline['source_revision'],plans={'local':make_plan(baseline,'local').model_dump(mode='json')}))
        for p in board_profiles():
            folder=ROOT/'tests/fixtures/board'/p['id']
            intent=json.loads((folder/'intent.json').read_text());mapping=json.loads((folder/'repo_map.json').read_text())
            values.append(dict(commit_sha=p['commit_sha'],intent=intent,repo_map=mapping,plans={'local':make_plan(intent,'local').model_dump(mode='json')},metrics={'backend':'test'},intent_checked=True))
        first=store.begin_deploy(project['project_id'],revision=values[0]['commit_sha'])
        store.save_plans(first,values[0]['plans']);store.set_status(first,'LIVE')
        with patch.object(self.runtime,'analyze',return_value=values[1]),patch.object(self.runtime,'command',side_effect=AssertionError('approval required')) as command:
            response=self.client.post(f"/api/projects/{project['project_id']}/deploy",json={'demo_version':'v3','targets':['local']})
        self.assertEqual(response.status_code,202)
        deployment=store.get_deployment(response.json()['deployment_id'])
        self.assertEqual(deployment['project_id'],project['project_id'])
        self.assertEqual(deployment['status'],'AWAITING_APPROVAL')
        self.assertTrue(any('worker' in reason for reason in deployment['approval_reasons']))
        command.assert_not_called()

    def test_branch_head_and_other_repository_cannot_bypass_target_limit(self):
        from control_plane.analysis import AnalysisFailed
        from control_plane.b_bridge import MappedSource
        from control_plane.runtime import LocalRuntime
        store=self.app.state.store
        folder=ROOT/'tests/fixtures/board/v3'
        mapped=MappedSource(ROOT/'.local/demo-board-sources/v3/snapshot',RepoMap.model_validate_json((folder/'repo_map.json').read_text()))
        for targets,repo in [(['local','onprem'],'demo-app'),(['local','aws'],'demo-app'),(['local'],'other')]:
            project=store.create_project('https://github.com/Team-InfraMorph/'+repo,'main',targets)
            did=store.begin_deploy(project['project_id'],targets=targets)
            runtime=LocalRuntime(root=self.root/did,b_modules=GitHubModules(),replay='unused.json')
            runtime.aws_config='unused';runtime.onprem_config='unused'
            with patch.object(runtime.b,'map',return_value=mapped),patch('control_plane.runtime.analysis_session',side_effect=AssertionError('must reject before AI')) as ai:
                with self.assertRaises(AnalysisFailed):runtime.analyze(store,store.get_deployment(did))
            ai.assert_not_called()


class ExecutionClassificationTests(unittest.TestCase):
    def test_worker_execution_failure_does_not_create_policy_block(self):
        from analyzer.e_worker import perform
        from policy_gate.reporting import rule
        def fail(data):raise RuntimeFailure('board_worker_timeout')
        with patch('analyzer.e_worker._perform',side_effect=fail):
            with self.assertRaises(RuntimeFailure) as failed:perform({'action':'deploy'})
        self.assertEqual(failed.exception.policy_results,[])

    def test_runtime_asset_bytes_must_match_and_mime_is_checked(self):
        import base64
        index=b'<h1>board</h1>'
        raw=json.dumps({'mime':'image/png','data':base64.b64encode(b'not a PNG').decode()}).encode()
        profile={'files':{'src/web/index.html':hashlib.sha256(index).hexdigest(),'src/web/assets/a.json':hashlib.sha256(raw).hexdigest()}}
        def request(url):return raw if url.endswith('a.json') else index
        with self.assertRaisesRegex(RuntimeFailure,'board_asset_invalid'):check_assets('http://127.0.0.1',profile,request)
        with self.assertRaisesRegex(RuntimeFailure,'board_asset_mismatch'):check_assets('http://127.0.0.1',profile,lambda u:b'other')

class BoardDocumentationTests(unittest.TestCase):
    def test_new_documents_links_and_contracts(self):
        from tests import test_policy_documentation as docs
        with patch.object(docs,'DOCS',ROOT/'policy_gate/docs/1.1.0'):
            docs.DocumentationContract().test_document_links_and_stable_sections()
        for name in ['I-000','L-001','P-001']:
            body=(ROOT/f'policy_gate/docs/1.1.0/rules/{name}.md').read_text()
            for line in body.splitlines():
                if 'schema_version' in line:self.assertNotIn('1.1.0',line)
        self.assertIn('board-profiles.json',(ROOT/'policy_gate/docs/1.1.0/board.md').read_text())
