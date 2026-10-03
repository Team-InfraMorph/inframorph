import assert from 'node:assert/strict';
import test from 'node:test';
import { describe, explain } from '../src/explain.js';

test('AWS preview failures explain the blocked change and next action', () => {
  for (const code of ['aws_plan_destructive_change', 'aws_plan_outside_app_module', 'aws_plan_unknown_action',
    'aws_worker_removal_requires_approval', 'aws_worker_removal_base_mismatch']) {
    const result = explain(code);
    assert.ok(result?.what);
    assert.ok(result?.next);
    assert.doesNotMatch(result.what, /비공개/);
  }
  assert.match(explain('aws_worker_removal_requires_approval').what, /worker 제거.*승인/);
  assert.match(explain('aws_plan_destructive_change').what, /적용 전에 중단/);
});

test('service activation reports the wait after the mandatory DB check', () => {
  assert.match(describe(JSON.stringify({ code: 'aws_services_activating',
    database: { bootstrap: 'reused', migration: 'executed', duration_ms: 50000 } })), /DB 준비.*기존 태스크/);
});
