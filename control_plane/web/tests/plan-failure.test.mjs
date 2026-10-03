import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {build} from 'esbuild';

const result = await build({
  entryPoints: [fileURLToPath(new URL('../src/Pipeline.jsx', import.meta.url))],
  bundle: true, write: false, platform: 'node', format: 'cjs', jsx: 'automatic',
  external: ['react', 'react/jsx-runtime'],
});
const mod = {exports: {}};
new Function('module', 'exports', 'require', result.outputFiles[0].text)(mod, mod.exports, createRequire(import.meta.url));
const {Pipeline, Results} = mod.exports;
const ts = '2026-10-03T01:22:00Z';
const deployment = {
  status: 'FAILED', targets: {local: {status: 'FAILED'}, aws: {status: 'FAILED'}},
  created_at: ts, updated_at: ts, analysis_metrics: {blocked_stage: 'plan_policy'},
};
const render = (Component, events, record = deployment) => renderToStaticMarkup(React.createElement(Component, {
  deployment: record, events, targets: ['local', 'aws'], ctx: {},
}));

for (const legacy of [false, true]) {
  test(`${legacy ? 'historical' : 'new'} plan rejection is a shared design failure`, () => {
    const events = ['local', 'aws'].flatMap((target, i) => [
      {seq: i * 2, ts, target, step: 'analyze', status: 'started'},
      {seq: i * 2 + 1, ts, target, step: legacy ? 'analyze' : 'plan', status: 'fail',
        detail: legacy ? 'AI 분석 실패: local_pipeline_analysis_failed' : '배포 설계 검사 실패: plan_config_mismatch'},
    ]);
    const html = render(Pipeline, events);
    assert.match(html, /실패: 공통 › 배포 설계 검사 단계에서 멈춤/);
    assert.doesNotMatch(html, /실패: 공통 › AI 분석/);
    assert.match(html, /n-ok[^]*?AI 분석/);
    assert.match(render(Results, events), /앞 단계에서 멈춰 배포하지 않음/);
    if (!legacy) assert.match(html, /환경설정이 배포 설계에 반영되지/);
  });
}

test('AWS Terraform plan failure remains a target-specific deployment preview failure', () => {
  const record = {...deployment, analysis_metrics: {}};
  const events = [{seq: 1, ts, target: 'aws', step: 'plan', status: 'fail', detail: 'terraform_plan_failed'}];
  const html = render(Pipeline, events, record);
  assert.match(html, /AWS › 변경 미리보기/);
  assert.doesNotMatch(html, /배포 설계 검사/);
  assert.match(render(Results, events, record), /AWS › 변경 미리보기/);
});
