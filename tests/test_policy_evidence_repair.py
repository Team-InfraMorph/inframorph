"""Evidence repair is a proposal, with host-enforced scope and unchanged gates."""
import asyncio
import copy
from contextlib import asynccontextmanager
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

from analyzer.backend import Reply, BackendError
from analyzer.config import Limits
from analyzer.runner import AnalysisResult, Metrics
from analyzer.snapshot import Snapshot
from analyzer.redaction import Redactor
from control_plane.auto_repair import repair, history, usage, reserve, RepairStopped
from control_plane.b_bridge import DemoModules
from control_plane.policy_results import read
from control_plane.runtime import LocalRuntime
from policy_gate.catalog import identity
from policy_gate.gate import PolicyError
from schemas import Intent, RepoMap
from tests import test_policy_auto_repair as existing
ROOT = existing.ROOT


class EvidenceRepairTests(unittest.IsolatedAsyncioTestCase):
    setUp = existing.RepairTests.setUp
    session = existing.RepairTests.session
    intent_gate = existing.RepairTests.intent_gate

    def worker_fixture(self):
        self.source = ROOT/'tests/fixtures/analyzer/v2/snapshot'
        self.mapping = RepoMap.model_validate_json((ROOT/'tests/fixtures/analyzer/v2/repo_map.json').read_text())
        self.intent = Intent.model_validate_json((ROOT/'schemas/fixtures/v2/intent.json').read_text())
        project = self.store.create_project('https://github.com/Team-InfraMorph/demo-app','main',['local'])
        self.did = self.store.begin_deploy(project['project_id'])
        self.store.set_commit(self.did, self.mapping.commit)
        self.stats['snapshot_digest'] = Snapshot(self.source, self.mapping.tree, self.limits, Redactor()).digest

    def bad(self, *, worker=False, db=True):
        value = self.intent.model_dump(mode='json')
        if db:
            value['state'][0]['evidence'] = ['prisma/schema.prisma:7']
        if worker:
            value['workloads'][1]['evidence'] = ['src/worker.js:1']
        return Intent.model_validate(value)

    async def run_case(self, initial, responses, *, session=None):
        try:
            self.intent_gate(initial, 0)
        except PolicyError as error:
            return await repair(store=self.store, did=self.did, mapping=self.mapping, source=self.source,
                stage='intent', target='local', initial=initial, error=error, validate=self.intent_gate,
                session=session or self.session(responses), limits=self.limits, initial_metrics=self.stats)
        self.fail('fault must fail the real source → Intent path')

    async def test_direct_db_evidence_repairs_and_links_real_inspections(self):
        fixed = await self.run_case(self.bad(), [self.intent.model_dump(mode='json')])
        self.assertEqual(fixed, self.intent)
        item = history(self.store, self.did)[0]
        reports = read(self.store, self.did)['results']
        self.assertEqual(item['repair_mode'], 'evidence_only')
        self.assertEqual(item['allowed_evidence_paths'], ['/state/0/evidence'])
        self.assertEqual(item['original_failure']['code'], 'db_provider_evidence_missing')
        self.assertIn(item['original_execution_id'], [r['execution_id'] for r in reports if r['decision'] == 'BLOCK'])
        self.assertEqual(next(r for r in reports if r['execution_id'] == item['last_recheck_execution_id'])['decision'], 'PASS')
        self.assertIsNone(item['stop_reason'])
        request = self.backend.requests[0]
        self.assertEqual(request['tools'], [])
        text = request['input'][0]['content']
        self.assertIn('src/server.js', text)  # Unchanged citations remain observable.
        self.assertIn('src/images.js', text)
        self.assertNotIn('package-lock.json', text)
        self.assertIn('db_provider', text)

    async def test_worker_requires_and_can_repair_both_roles(self):
        self.worker_fixture()
        await self.run_case(self.bad(worker=True, db=False), [self.intent.model_dump(mode='json')])
        record = history(self.store, self.did)[0]
        self.assertEqual(record['allowed_evidence_paths'], ['/workloads/1/evidence'])
        self.assertEqual(record['original_failure']['code'], 'worker_command_evidence_missing')

    async def test_scope_rejects_other_fields_even_when_valid_json(self):
        mutations = [lambda v: v['config'].update(PORT='3000'),
                     lambda v: v['state'][0].update(engine='postgresql'),
                     lambda v: v['state'][1].update(reason='changed reasoning'),
                     lambda v: v['workloads'][0].update(public=False),
                     lambda v: v['secrets'].append('JWT_SECRET'),
                     lambda v: v.update(source_revision='a'*40)]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                # Every candidate gets an independent deployment/budget.
                project = self.store.create_project('https://github.com/Team-InfraMorph/demo-app','main',['local'])
                self.did = self.store.begin_deploy(project['project_id'])
                self.store.set_commit(self.did, self.mapping.commit)
                value = self.intent.model_dump(mode='json')
                mutate(value)
                with self.assertRaisesRegex(RepairStopped, 'evidence_repair_scope_violation'):
                    await self.run_case(self.bad(), [value])
                self.assertEqual(len(self.backend.requests), 1)
                row = history(self.store, self.did)[0]
                self.assertEqual(row['stop_reason'], 'evidence_repair_scope_violation')
                self.assertEqual(row['recheck_execution_ids'], [])

    async def test_partial_evidence_proposal_does_not_expand_scope(self):
        self.worker_fixture()
        initial = self.bad(worker=True, db=False)
        partial = initial.model_dump(mode='json')
        partial['workloads'][1]['evidence'] = ['package.json:12']
        changed = self.intent.model_dump(mode='json')
        changed['workloads'][1]['command'] = 'node src/server.js'
        with self.assertRaisesRegex(RepairStopped, 'evidence_repair_scope_violation'):
            await self.run_case(initial, [partial, changed])
        rows = history(self.store, self.did)
        self.assertEqual([r['result_code'] for r in rows],
                         ['worker_start_evidence_missing', 'evidence_repair_scope_violation'])
        self.assertEqual(rows[0]['original_execution_id'], rows[1]['original_execution_id'])

    async def test_sequential_host_failures_allow_only_corresponding_paths(self):
        self.worker_fixture()
        initial = self.bad(worker=True)
        first = initial.model_dump(mode='json')
        first['state'][0]['evidence'] = self.intent.model_dump()['state'][0]['evidence']
        # Rule order is DB first, then worker; the second path is authorized by
        # the host's second failure, not by the first model proposal.
        result = await self.run_case(initial, [first, self.intent.model_dump(mode='json')])
        self.assertEqual(result, self.intent)
        rows = history(self.store, self.did)
        self.assertEqual(rows[1]['allowed_evidence_paths'], ['/state/0/evidence', '/workloads/1/evidence'])

    async def test_legacy_followup_evidence_failure_can_extend_strict_scope(self):
        self.worker_fixture()
        raw = self.bad(worker=True).model_dump(mode='json')
        raw['workloads'][1]['evidence'] = ['src/server.js:22']
        initial = Intent.model_validate(raw)
        first = initial.model_dump(mode='json')
        first['state'][0]['evidence'] = ['prisma/schema.prisma:6']
        result = await self.run_case(initial, [first, self.intent.model_dump(mode='json')])
        self.assertEqual(result, self.intent)
        rows = history(self.store, self.did)
        self.assertEqual(rows[0]['result_code'], 'worker_evidence_unrelated')
        self.assertEqual(rows[1]['allowed_evidence_paths'], ['/state/0/evidence', '/workloads/1/evidence'])
        self.assertTrue(all(row['repair_mode'] == 'evidence_only' for row in rows))

    async def test_unknown_usage_and_original_binding_are_persisted_before_provider_call(self):
        session = self.session([])
        async def respond(**request):
            row = history(self.store, self.did)[0]
            self.assertFalse(row['metrics']['usage_complete'])
            self.assertEqual(row['metrics']['model_calls'], 1)
            self.assertEqual(row['original_failure']['code'], 'db_provider_evidence_missing')
            self.assertIsNotNone(row['original_execution_id'])
            self.assertFalse(usage(self.store, self.did)['usage_complete'])
            return Reply(text=self.intent.model_dump_json(), input_tokens=1, output_tokens=1)
        self.backend.respond = respond
        await self.run_case(self.bad(), [], session=session)
        self.assertTrue(usage(self.store, self.did)['usage_complete'])

    def test_interrupted_old_payload_cannot_claim_complete_usage(self):
        from control_plane.db import Store
        reserve(self.store, self.did, self.mapping, self.digest, 'local', 'intent', 'db_provider_evidence_missing')
        payload = {'metrics': {'usage_complete': True, 'model_calls': 0},
                   'original_failure': {'code': 'db_provider_evidence_missing'},
                   'original_execution_id': 'preserved-origin'}
        with self.store._lock, self.store._conn:
            self.store._conn.execute('UPDATE policy_repairs SET payload=? WHERE deployment_id=?',
                                     (json.dumps(payload), self.did))
        reopened = Store(self.store.path)
        self.addCleanup(reopened.close)
        reopened.recover_interrupted()
        row = history(reopened, self.did)[0]
        self.assertEqual(row['status'], 'interrupted')
        self.assertEqual(row['original_execution_id'], 'preserved-origin')
        self.assertFalse(usage(reopened, self.did)['usage_complete'])

    async def test_repeat_exhaustion_preserves_block_and_stop_reason(self):
        bad = self.bad()
        with self.assertRaisesRegex(RepairStopped, 'db_provider_evidence_missing'):
            await self.run_case(bad, [bad.model_dump(mode='json')]*3)
        rows = history(self.store, self.did)
        self.assertEqual(len(rows), 3)
        self.assertTrue(rows[-1]['attempts_exhausted'])
        self.assertEqual(rows[-1]['stop_reason'], 'db_provider_evidence_missing')
        self.assertEqual(read(self.store, self.did)['results'][-1]['decision'], 'BLOCK')

    async def test_provider_timeout_keeps_original_violation_distinct(self):
        with self.assertRaisesRegex(RepairStopped, 'api_timeout'):
            await self.run_case(self.bad(), [BackendError('api_timeout')])
        row = history(self.store, self.did)[0]
        self.assertEqual(row['original_failure']['code'], 'db_provider_evidence_missing')
        self.assertEqual(row['stop_reason'], 'api_timeout')
        self.assertIsNone(row['last_recheck_execution_id'])

    async def test_policy_change_while_waiting_discards_candidate(self):
        before = identity()
        backend = self.session([])
        async def respond(**request):
            fake['policy_digest'] = 'f'*64
            return Reply(text=self.intent.model_dump_json(), input_tokens=1, output_tokens=1)
        self.backend.respond = respond
        fake = copy.deepcopy(before)
        with patch('policy_gate.catalog.identity', side_effect=lambda: fake):
            with self.assertRaisesRegex(RepairStopped, 'policy_repair_policy_changed'):
                await self.run_case(self.bad(), [], session=backend)
        self.assertEqual(history(self.store,self.did)[0]['recheck_execution_ids'], [])

    async def test_source_change_while_waiting_discards_candidate(self):
        copied = self.root/'source'
        shutil.copytree(self.source, copied)
        self.source = copied
        session = self.session([])
        async def respond(**request):
            with (copied/'src/server.js').open('a') as output:
                output.write('\n// concurrent source mutation\n')
            return Reply(text=self.intent.model_dump_json(), input_tokens=1, output_tokens=1)
        self.backend.respond = respond
        with self.assertRaisesRegex(RepairStopped, 'policy_repair_source_changed'):
            await self.run_case(self.bad(), [], session=session)
        self.assertEqual(history(self.store,self.did)[0]['recheck_execution_ids'], [])

    async def test_input_limit_fails_without_silent_truncation_or_model_call(self):
        self.limits = Limits(max_request_bytes=100)
        with self.assertRaisesRegex(RepairStopped, 'policy_repair_input_limit'):
            await self.run_case(self.bad(), [self.intent.model_dump(mode='json')])
        self.assertEqual(len(self.backend.requests), 0)
        self.assertEqual(history(self.store,self.did)[0]['stop_reason'], 'policy_repair_input_limit')


class EvidenceRuntimeTests(unittest.TestCase):
    def test_exhausted_intent_repair_never_calls_planner_or_creates_execution_context(self):
        import tempfile
        from control_plane.db import Store
        from tests.test_policy_auto_repair import Backend
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            store = Store(root/'db.sqlite')
            self.addCleanup(store.close)
            project = store.create_project('https://github.com/Team-InfraMorph/demo-app','main',['local'])
            did = store.begin_deploy(project['project_id'])
            raw = json.loads((ROOT/'schemas/fixtures/v1/intent.json').read_text())
            raw['state'][0]['evidence'] = ['prisma/schema.prisma:7']
            bad = Intent.model_validate(raw)
            runtime = LocalRuntime(root=root/'runtime', b_modules=DemoModules())
            backend = Backend([raw]*3)
            @asynccontextmanager
            async def session(*args):
                yield backend
            async def analyze(kind, model, replay, mapped):
                digest = Snapshot(mapped.snapshot,mapped.repo_map.tree,Limits(),Redactor()).digest
                return AnalysisResult(bad,Metrics(backend='replay',snapshot_digest=digest))
            with patch('control_plane.runtime.analyze_source', side_effect=analyze), \
                    patch('control_plane.runtime.analysis_session', side_effect=session), \
                    patch.object(runtime.b, 'plan') as planner:
                with self.assertRaisesRegex(Exception, 'db_provider_evidence_missing'):
                    runtime.analyze(store, store.get_deployment(did))
                planner.assert_not_called()
            self.assertEqual(len(history(store,did)), 3)
            self.assertFalse(runtime.context_file(did).exists())
            events=store._all("SELECT payload FROM policy_events WHERE deployment_id=? AND event='policy_repair_stopped'",(did,))
            self.assertEqual(json.loads(events[-1]['payload'])['original_failure'],'db_provider_evidence_missing')
