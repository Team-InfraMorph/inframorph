import {readFile} from 'node:fs/promises';
import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import {createRequire} from 'node:module';
import {renderToStaticMarkup} from 'react-dom/server';
import {transformWithEsbuild} from 'vite';
import {build} from 'esbuild';
import {fileURLToPath} from 'node:url';
const bundle = await build({entryPoints:[fileURLToPath(new URL('../src/PolicyCard.jsx',import.meta.url))],bundle:true,write:false,platform:'node',format:'cjs',jsx:'automatic',external:['react','react/jsx-runtime']});
const mod = {exports:{}};
new Function('module','exports','require',bundle.outputFiles[0].text)(mod,mod.exports,createRequire(import.meta.url));
const Card = mod.exports.default;
const render = props => renderToStaticMarkup(React.createElement(Card,props));
const row = {seq:1,target:'local',attempt:0,stage:'patch',decision:'BLOCK',complete:true,rule_id:'P-003',title:'모델 삭제',remedy:'모델 보존',version:'2.2.0',reason_code:'prisma_structure_changed',path:'prisma/schema.prisma'};
test('missing results never imply success',()=>{assert.match(render({}),/아직 기록된/); assert.doesNotMatch(render({}),/policy-pass/);});
test('failure shows rule, evidence and remedy',()=>{const html=render({data:{results:[row]}});for(const term of ['차단','P-003','prisma/schema.prisma','모델 보존']) assert.ok(html.includes(term));});
test('retry and targets remain distinct',()=>{const html=render({data:{results:[row,{...row,seq:2,target:'aws',attempt:1,decision:'PASS'}]}});for(const term of ['Local 테스트','AWS','최초 검사','재시도']) assert.ok(html.includes(term));});
test('unsupported and error are separate from blocked',()=>{const html=render({data:{results:[{...row,decision:'UNSUPPORTED',complete:false},{...row,seq:2,decision:'ERROR'}]}});assert.match(html,/미지원/);assert.match(html,/검사 오류/);assert.match(html,/검사 미완료/);});
test('source-controlled text is escaped',()=>{const html=render({data:{results:[{...row,path:'<img src=x onerror=alert(1)>',title:'<script>bad</script>'}]}});assert.doesNotMatch(html,/<script>|<img/);assert.match(html,/&lt;script/);});
test('failed refresh does not render stale success',()=>{const html=render({error:true,data:{results:[{...row,decision:'PASS'}]}});assert.match(html,/불러오지 못/);assert.doesNotMatch(html,/policy-pass/);});
test('policy repairs show all three attempts and distinguish validation from deployment',()=>{
 const repairs=[1,2,3].map(attempt=>({attempt,target:'local',stage:'patch',reason:'javascript_syntax_invalid',status:attempt===3?'passed':'failed',result_code:attempt===3?'policy_repair_validated':'javascript_syntax_invalid',diff:'-broken\n+fixed',metrics:{model_calls:1,input_tokens:20,output_tokens:10}}));
 const html=render({data:{results:[row],repairs,max_repair_attempts:3}});
 for(const value of ['3 / 최대 3회','1회','2회','3회','정책 재검사 통과','Local 테스트','-broken','+fixed'])assert.ok(html.includes(value),value);
 assert.doesNotMatch(html,/배포 성공/);
});
test('repair proposal text is escaped and terminal reason is visible',()=>{
 const html=render({data:{repairs:[{attempt:3,stage:'intent',target:'local',status:'failed',reason:'intent_source_mismatch',result_code:'policy_repair_limit_reached',diff:'<script>bad</script>'}]}});
 assert.match(html,/자동 수정이 중단/);assert.match(html,/policy_repair_limit_reached/);assert.doesNotMatch(html,/<script>/);
});

const managed={...row,family:'inframorph-policy',version:'1.0.0',status:'development',execution_id:'run-1',policy_digest:'abc',checkpoint:'build',rules:[{rule_id:'I-003',decision:'BLOCK',title:'DB 근거',expected:'DB provider 일치',evidence:{observed:'sqlite',claimed:['postgresql']},remedy:'DB 요구 수정'}],required_rules:['I-003'],evaluated_rules:['I-003']};
test('managed version displays without development label or legacy history',()=>{const html=render({data:{results:[managed]}});assert.match(html,/1\.0\.0/);assert.doesNotMatch(html,/개발 중|개발중/);assert.doesNotMatch(html,/단계별 요약만 보존됨/);});
test('observed and expected evidence are explained',()=>{const html=render({data:{results:[managed]}});for(const value of ['기대 조건','관찰값: sqlite','분석값: postgresql','규칙 설명'])assert.ok(html.includes(value));});
test('a passed stage does not make an incomplete deployment pass',()=>{const html=render({data:{results:[{...managed,decision:'PASS'}],summaries:[{target:'local',decision:'NOT_RUN',complete:false}]}});assert.match(html,/전체 필수 검사 기록이 완료되지/);});
test('expired diagnostics retain their timeline',()=>{const html=render({history:{events:[{seq:1,event:'inspection_interrupted',payload:{}}],diagnostics:[{id:'log',payload:null}],failure_reviews:[]}});assert.match(html,/검사 중단 · 결과 없음/);assert.match(html,/상세 로그 보존 기간 만료/);});
test('failure review and original failure remain separately visible',()=>{const html=render({data:{results:[managed]},history:{events:[],diagnostics:[],failure_reviews:[{id:'review',execution_id:'run-1',payload:{classification:'false_positive',summary:'회귀 근거',regression_test:'tests/example',resolved_execution_id:'run-2'}}]}});for(const word of ['차단','확인된 오탐','run-2'])assert.ok(html.includes(word));});
test('diagnostic HTML is plain escaped text',()=>{const html=render({history:{events:[],diagnostics:[{id:'x',payload:{text:'<script>bad</script>'}}],failure_reviews:[]}});assert.doesNotMatch(html,/<script>/);assert.match(html,/&lt;script/);});

const patchSource = await readFile(new URL('../src/Patch.jsx', import.meta.url), 'utf8');
const patchCompiled = await transformWithEsbuild(patchSource, 'Patch.jsx', {jsx:'transform', jsxFactory:'React.createElement', format:'cjs'});
const patchModule = {exports:{}};
new Function('module','React','require',patchCompiled.code)(patchModule,React,createRequire(import.meta.url));
const renderPatch = props => renderToStaticMarkup(React.createElement(patchModule.exports.PatchCard,props));
test('missing patch stays visible and never claims unchanged',()=>{assert.match(renderPatch({}),/아직 저장된 코드 변경 내역이 없습니다/);});
test('patch loading and failure are distinguished',()=>{assert.match(renderPatch({loading:true}),/불러오는 중/);assert.match(renderPatch({error:true}),/불러오지 못/);});
test('actual patch retains file names and complete diff',()=>{const html=renderPatch({patch:{local:{files:[{path:'src/app.js',action:'modify',diff:'-old\n+new'}]}}}); for(const value of ['app.js','-old','+new','전체 diff 보기']) assert.ok(html.includes(value));});

const groupHistory=mod.exports.historyGroups;
test('history groups by execution ID and preserves event sequence',()=>{
 const groups=groupHistory([{seq:3,execution_id:'b',event:'inspection_started',payload:{}},{seq:2,execution_id:'a',event:'inspection_finished',payload:{decision:'PASS'}},{seq:1,execution_id:'a',event:'inspection_started',payload:{checkpoint:'patch'}}]);
 assert.equal(groups.length,2);assert.deepEqual(groups[0].events.map(e=>e.seq),[1,2]);assert.equal(groups[0].decision,'PASS');assert.equal(groups[1].pending,true);
});
test('passing a rule cannot make an unfinished execution pass',()=>{
 const groups=groupHistory([{seq:1,execution_id:'a',event:'inspection_started',payload:{}},{seq:2,execution_id:'a',event:'rule_evaluated',payload:{decision:'PASS'}}]);assert.equal(groups[0].decision,null);assert.equal(groups[0].pending,true);
});
test('interruption overrides an earlier pass summary',()=>{
 const groups=groupHistory([{seq:1,execution_id:'a',event:'inspection_interrupted',payload:{}}],[{execution_id:'a',decision:'PASS'}]);assert.equal(groups[0].decision,'ERROR');
});
test('history distinguishes execution count from event count',()=>{
 const html=render({history:{events:[{seq:1,execution_id:'a',event:'inspection_started',payload:{}},{seq:2,execution_id:'a',event:'inspection_finished',payload:{decision:'PASS'}}]}});for(const text of ['검사 1회','이벤트 2건','전체 로그','검사별 보기'])assert.ok(html.includes(text));
});
test('rule help uses policy version for new and historical records',()=>{
 for(const extra of [{},{document_revision:1},{document_revision:4}]) {
  const html=render({data:{results:[{...managed,...extra}]}});
  assert.match(html,/rules\/I-003\?section=remedy/);
  assert.doesNotMatch(html,/revision=/);
 }
});

test('three targets use main labels without promoting incomplete onprem checks',()=>{
 const html=render({data:{results:['aws','onprem','local'].map((target,i)=>({...managed,seq:i+1,target})),summaries:[{target:'local',decision:'NOT_RUN',complete:false},{target:'onprem',decision:'NOT_RUN',complete:false}]}});
 assert.ok(html.indexOf('Local 테스트 (노트북)')<html.indexOf('온프레미스 (사내 서버)'));
 assert.ok(html.indexOf('온프레미스 (사내 서버)')<html.indexOf('AWS (서울)'));
 assert.match(html,/data-target="onprem"[^]*?온프레미스 \(사내 서버\) · 배포 당시 미실행/);
});

test('policy update run stays distinct from original deployment status',()=>{
 const html=render({data:{results:[{...managed,checkpoint:'policy_update',purpose:'policy_update',decision:'PASS'}],summaries:[{target:'local',decision:'BLOCK',complete:true}]}});
 assert.match(html,/배포 당시 차단/);
 assert.match(html,/정책 재검사/);
});
