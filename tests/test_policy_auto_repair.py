import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from analyzer.backend import Reply, BackendError
from analyzer.config import Limits
from analyzer.recovery import PatchedCandidate, Approval
from analyzer.runner import AnalysisResult, Metrics
from analyzer.redaction import Redactor
from analyzer.snapshot import Snapshot
from analyzer.source_policy import SourcePolicyError, validate_demo_intent, validate_demo_plan
from code_patch.runner import patch_snapshot, read_snapshot, transform, ALLOWED_PATHS
from control_plane.auto_repair import repair, history, usage, reserve, finish, approved_local_patch, RepairStopped
from control_plane.b_bridge import DemoModules
from control_plane.db import Store
from control_plane.policy_results import check, read
from control_plane.runtime import LocalRuntime
from policy_gate.gate import PolicyError, validate_intent, validate_plan, validate_patch
from schemas import Intent, Plan, RepoMap

ROOT = Path(__file__).resolve().parents[1]


class Backend:
    name = 'openai'
    known_secrets = ('repair-private-key-canary',)

    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    async def respond(self, **request):
        self.requests.append(request)
        value = next(self.replies)
        if isinstance(value, BaseException):
            raise value
        return Reply(text=json.dumps(value), input_tokens=100, output_tokens=50)


class RepairTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root/'cp.db')
        self.addCleanup(self.store.close)
        self.project = self.store.create_project('https://github.com/Team-InfraMorph/demo-app','main',['local','aws'])
        self.did = self.store.begin_deploy(self.project['project_id'])
        self.source = ROOT/'tests/fixtures/analyzer/v1/snapshot'
        self.mapping = RepoMap.model_validate_json((ROOT/'tests/fixtures/analyzer/v1/repo_map.json').read_text())
        self.store.set_commit(self.did, self.mapping.commit)
        self.intent = Intent.model_validate_json((ROOT/'schemas/fixtures/v1/intent.json').read_text())
        self.plan = Plan.model_validate_json((ROOT/'schemas/fixtures/v1/plan.local.json').read_text())
        self.limits = Limits(max_estimated_usd=.1)
        self.digest = Snapshot(self.source,self.mapping.tree,self.limits,Redactor()).digest
        self.stats = {'backend':'openai','model':'gpt-6-luna','snapshot_digest':self.digest,'usage_complete':True}

    def session(self, responses):
        self.backend = Backend(responses)
        @asynccontextmanager
        async def session():
            yield self.backend
        return session

    def intent_gate(self, value, attempt):
        check(self.store,self.did,'local','source',lambda:validate_demo_intent(value,self.source,self.mapping),attempt=attempt)
        check(self.store,self.did,'local','intent',lambda:validate_intent(value,self.source,self.mapping.commit),attempt=attempt)

    async def run_intent(self, responses, **extra):
        initial = self.intent.model_copy(update={'unknowns':['port is unknown']})
        return await repair(store=self.store,did=self.did,mapping=self.mapping,source=self.source,
            stage='intent',target='local',initial=initial,error=SourcePolicyError('intent_source_mismatch',['unknowns']),
            validate=self.intent_gate,session=self.session(responses),limits=self.limits,initial_metrics=self.stats,**extra)

    async def test_third_proposal_passes_real_gates_and_source_is_unchanged(self):
        wrong = self.intent.model_dump(mode='json') | {'unknowns':['still uncertain']}
        result = await self.run_intent([wrong,wrong,self.intent.model_dump(mode='json')])
        self.assertEqual(result,self.intent)
        rows = history(self.store,self.did)
        self.assertEqual([r['status'] for r in rows],['failed','failed','passed'])
        self.assertEqual([r['attempt'] for r in rows],[1,2,3])
        self.assertEqual(len(self.backend.requests),3)
        self.assertEqual(usage(self.store,self.did)['api_calls'],3)
        self.assertEqual(Snapshot(self.source,self.mapping.tree,self.limits,Redactor()).digest,self.digest)
        self.assertTrue(rows[0]['diff'])
        self.assertEqual(rows[1]['diff'],'')  # An unchanged proposal still spends an attempt.
        self.assertTrue(rows[2]['diff'])
        self.assertEqual(read(self.store,self.did)['max_repair_attempts'],3)
        self.assertEqual({r['attempt'] for r in read(self.store,self.did)['results']},{1,2,3})

    async def test_three_failures_stop_and_a_reopened_store_cannot_reset_budget(self):
        wrong = self.intent.model_dump(mode='json') | {'unknowns':['still uncertain']}
        with self.assertRaisesRegex(RepairStopped,'intent_source_mismatch'):
            await self.run_intent([wrong]*4)
        self.assertEqual(len(self.backend.requests),3)
        reopened = Store(self.store.path)
        self.addCleanup(reopened.close)
        with self.assertRaisesRegex(RepairStopped,'limit_reached'):
            reserve(reopened,self.did,self.mapping,self.digest,'aws','plan','plan_config_mismatch')
        self.assertEqual(len(history(reopened,self.did)),3)

    async def test_invalid_json_schema_counts_as_attempt_and_can_recover(self):
        result = await self.run_intent([{'unexpected':'field'}, {}, self.intent.model_dump(mode='json')])
        self.assertEqual(result,self.intent)
        self.assertEqual([r['result_code'] for r in history(self.store,self.did)],
                         ['policy_repair_invalid_response','policy_repair_invalid_response','policy_repair_validated'])

    async def test_policy_stages_share_three_attempts(self):
        await self.run_intent([self.intent.model_dump(mode='json')])
        wrong = self.plan.model_copy(update={'config':{'STORAGE_DRIVER':'fs','PORT':'3001'}})
        def gate(value, attempt):
            check(self.store,self.did,'local','plan',lambda:(validate_demo_plan(value,self.mapping),validate_plan(self.intent,value)),attempt=attempt)
        result = await repair(store=self.store,did=self.did,mapping=self.mapping,source=self.source,
            stage='plan',target='local',initial=wrong,error=PolicyError('plan_config_mismatch'),
            validate=gate,session=self.session([wrong.model_dump(mode='json'), self.plan.model_dump(mode='json')]),
            limits=self.limits,initial_metrics=self.stats,intent=self.intent)
        self.assertEqual(result,self.plan)
        self.assertEqual([r['stage'] for r in history(self.store,self.did)],['intent','plan','plan'])
        with self.assertRaisesRegex(RepairStopped,'limit_reached'):
            reserve(self.store,self.did,self.mapping,self.digest,'local','patch','patch_behavior_changed')

    def bad_patch(self):
        original,_ = read_snapshot(self.source,self.mapping)
        self.expected = transform(original,self.plan)
        modified = {n:self.expected[n] for n in ALLOWED_PATHS if n in self.expected}
        modified['src/images.js'] += b'\nfunction broken( {\n'
        folder = self.root/'initial-patch'
        manifest = patch_snapshot(self.source,self.mapping,self.plan,folder,replacements=modified)
        return PatchedCandidate(folder,manifest,self.plan,tuple(sorted(original.keys()|modified.keys())))

    async def repair_patch(self, candidate, responses):
        def gate(value, attempt):
            check(self.store,self.did,'local','patch',lambda:validate_patch(self.source,value.directory,value.plan),attempt=attempt)
        return await repair(store=self.store,did=self.did,mapping=self.mapping,source=self.source,
            stage='patch',target='local',initial=candidate,error=PolicyError('javascript_syntax_invalid'),
            validate=gate,session=self.session(responses),limits=self.limits,initial_metrics=self.stats,output_dir=self.root)

    async def test_patch_repair_is_revalidated_and_aws_uses_same_tested_source(self):
        candidate = self.bad_patch()
        with self.assertRaisesRegex(PolicyError,'javascript_syntax_invalid'):
            validate_patch(self.source,candidate.directory,self.plan)
        fixed = await self.repair_patch(candidate,[{'files':[{'path':'src/images.js','content':self.expected['src/images.js'].decode()}]}])
        diff = history(self.store,self.did)[0]['diff']
        self.assertIn('before/src/images.js',diff)
        self.assertNotIn('package.json',diff)
        self.assertNotIn('schema.prisma',diff)
        validate_patch(self.source,fixed.directory,self.plan)
        context = SimpleNamespace(deployment_id=self.did,output_dir=str(self.root),plan=self.plan,
                                  repo_map=self.mapping,snapshot=str(self.source))
        aws = Plan.model_validate_json((ROOT/'schemas/fixtures/v1/plan.aws.json').read_text())
        self.store.save_validated_patch(self.did,fixed,self.mapping,Approval(approved=True,fingerprint=fixed.fingerprint),'initial')
        with self.assertRaisesRegex(RepairStopped,'local_test_required'):
            approved_local_patch(self.store,context,aws,self.root/'aws-patch')
        self.store.mark_patch_applied(self.did,'initial')
        manifest = approved_local_patch(self.store,context,aws,self.root/'aws-patch')
        validate_patch(self.source,self.root/'aws-patch',aws)
        self.assertEqual(manifest['patched_digest'],fixed.manifest['patched_digest'])
        self.assertEqual(manifest['target'],'aws')
        (fixed.directory/'source/src/images.js').write_text('tampered')
        with self.assertRaises(ValueError):
            approved_local_patch(self.store,context,aws,self.root/'changed-patch')

    async def test_injected_path_cannot_edit_host_or_original(self):
        candidate = self.bad_patch()
        with self.assertRaisesRegex(RepairStopped,'path_forbidden'):
            await self.repair_patch(candidate,[{'files':[{'path':'../../.env','content':'stolen'}]}])
        self.assertFalse((self.root/'policy-repair-1').exists())
        self.assertEqual(len(self.backend.requests),1)

    async def test_forbidden_code_is_blocked_by_real_gate_and_not_retried(self):
        candidate = self.bad_patch()
        with self.assertRaises(RepairStopped):
            await self.repair_patch(candidate,[{'files':[{'path':'src/images.js','content':"require('child_process');"}]}])
        self.assertEqual(history(self.store,self.did)[0]['status'],'failed')
        self.assertEqual(len(self.backend.requests),1)
        self.assertEqual(self.store.get_runtime_patches(self.did),{})

    async def test_initial_security_and_unsupported_failures_never_call_model(self):
        for code in ['unreviewed_runtime_source','artifact_digest_mismatch','forbidden_code_pattern','secret_in_source']:
            error = SourcePolicyError(code) if code=='unreviewed_runtime_source' else PolicyError(code)
            with self.assertRaises(type(error)):
                await repair(store=self.store,did=self.did,mapping=self.mapping,source=self.source,
                    stage='intent',target='local',initial=self.intent,error=error,validate=self.intent_gate,
                    session=self.session([]),limits=self.limits,initial_metrics=self.stats)
            self.assertEqual(self.backend.requests,[])
        self.assertEqual(history(self.store,self.did),[])

    async def test_secret_response_is_not_saved_or_retried(self):
        unsafe = self.intent.model_dump(mode='json') | {'unknowns':['repair-private-key-canary']}
        with self.assertRaisesRegex(RepairStopped,'unsafe_output'):
            await self.run_intent([unsafe])
        self.assertNotIn('repair-private-key-canary',json.dumps(history(self.store,self.did)))

    async def test_unknown_usage_and_budget_exhaustion_stop(self):
        with self.assertRaisesRegex(RepairStopped,'api_timeout'):
            await self.run_intent([BackendError('api_timeout')])
        self.assertFalse(usage(self.store,self.did)['usage_complete'])
        with self.assertRaisesRegex(RepairStopped,'usage_unknown'):
            await self.run_intent([self.intent.model_dump(mode='json')])
        self.assertEqual(self.backend.requests,[])

    async def test_budget_is_checked_before_request(self):
        self.stats['estimated_usd'] = .1
        with self.assertRaisesRegex(RepairStopped,'budget_exhausted'):
            await self.run_intent([self.intent.model_dump(mode='json')])
        self.assertEqual(self.backend.requests,[])

    async def test_concurrent_claim_and_interruption_do_not_restore_budget(self):
        reserve(self.store,self.did,self.mapping,self.digest,'local','intent','intent_source_mismatch')
        other = Store(self.store.path)
        self.addCleanup(other.close)
        with self.assertRaisesRegex(RepairStopped,'limit_reached'):
            reserve(other,self.did,self.mapping,self.digest,'aws','plan','plan_config_mismatch')
        other.recover_interrupted()
        self.assertEqual(history(other,self.did)[0]['status'],'interrupted')
        self.assertFalse(usage(other,self.did)['usage_complete'])


class RuntimeRepairTests(unittest.TestCase):
    def test_runtime_repairs_intent_then_plan_before_saving_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            store = Store(root/'db.sqlite')
            self.addCleanup(store.close)
            project = store.create_project('https://github.com/Team-InfraMorph/demo-app','main',['local'])
            did = store.begin_deploy(project['project_id'])
            good = Intent.model_validate_json((ROOT/'schemas/fixtures/v1/intent.json').read_text())
            bad = good.model_copy(update={'unknowns':['runtime secret value is unknown']})
            plan = Plan.model_validate_json((ROOT/'schemas/fixtures/v1/plan.local.json').read_text())
            runtime = LocalRuntime(root=root/'runtime',b_modules=DemoModules())
            backend = Backend([good.model_dump(mode='json'),plan.model_dump(mode='json')])
            @asynccontextmanager
            async def session(*args):
                yield backend
            async def analysis(kind,model,replay,mapped):
                digest = Snapshot(mapped.snapshot,mapped.repo_map.tree,Limits(),Redactor()).digest
                return AnalysisResult(bad,Metrics(backend='replay',snapshot_digest=digest))
            with patch('control_plane.runtime.analyze_source',side_effect=analysis), \
                    patch('control_plane.runtime.analysis_session',side_effect=session), \
                    patch.object(runtime.b,'plan',return_value=plan.model_copy(update={'config':{'PORT':'3001','STORAGE_DRIVER':'fs'}})):
                result = runtime.analyze(store,store.get_deployment(did))
            self.assertEqual(result['intent']['unknowns'],[])
            self.assertEqual(result['plans']['local'],plan.model_dump(mode='json'))
            self.assertEqual([r['stage'] for r in history(store,did)],['intent','plan'])
            self.assertTrue(runtime.context_file(did).is_file())
