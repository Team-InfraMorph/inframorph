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

test('GCP preview failures and private adapter failures have actionable Korean explanations', () => {
  for (const code of ['gcp_plan_destructive_change', 'gcp_plan_outside_app_module',
    'gcp_plan_unknown_action', 'gcp_context_not_approved']) {
    const result = explain(code);
    assert.ok(result?.what);
    assert.ok(result?.next);
  }
  assert.match(explain('gcp_adapter_failed').what, /GCP 배포/);
  assert.match(describe('publishing immutable Artifact Registry tag'), /Artifact Registry/);
  assert.match(describe('waiting for the latest revision to serve all traffic'), /Cloud Run/);
});
