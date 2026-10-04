import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import {createRequire} from 'node:module';
import {renderToStaticMarkup} from 'react-dom/server';
import {build} from 'esbuild';
import {fileURLToPath} from 'node:url';
async function load(name){
 const bundle=await build({entryPoints:[fileURLToPath(new URL(`../src/${name}`,import.meta.url))],bundle:true,write:false,platform:'node',format:'cjs',jsx:'automatic',external:['react','react/jsx-runtime']});
 const mod={exports:{}};new Function('module','exports','require',bundle.outputFiles[0].text)(mod,mod.exports,createRequire(import.meta.url));return mod.exports;
}
const {PolicyChange,PolicyImpactSummary,PolicyReviewContext,PolicyImpactActions}=await load('PolicyChanges.jsx');
const {EvidenceRequirements,RepairHistory}=await load('PolicyCard.jsx');
const render=(Component,props)=>renderToStaticMarkup(React.createElement(Component,props));
const change={id:'db',title:'DB 직접 근거',before_condition:'datasource 범위 인용',after_condition:'provider 토큰 인용',reason:'분석 재현',applies:'DB 사용 배포',actions:['직접 인용 보완'],rule_ids:['I-003'],recheck:true,review:false,redeploy:false,examples:[{id:'url',before_decision:'PASS',after_decision:'BLOCK',description:'URL만 인용'}]};
test('change comparison explains conditions before implementation references',()=>{
 const html=render(PolicyChange,{change});
 for(const text of ['이전 조건','datasource 범위 인용','새 조건','provider 토큰 인용','DB 사용 배포','통과 → 차단','직접 인용 보완'])assert.ok(html.includes(text),text);
 assert.ok(html.indexOf('이전 조건')<html.indexOf('구현·검증 연결'));assert.match(html,/rules\/I-003\?section=decisions/);
});
const item={original_decision:'PASS',policy:{version:'1.0.0',policy_digest:'old-digest'},active:{version:'1.1.0'},status:'LIVE',impact:{advisory:true},latest_recheck:{decision:'BLOCK',reason_code:'db_provider_evidence_missing'},action:'evidence_confirmation_required'};
test('historical pass current recommendation and service state are independent',()=>{
 const html=render(PolicyImpactSummary,{item});
 for(const text of ['배포 당시 판정','통과','근거 보완 권고','LIVE','원래 기록 유지','base_policy_digest=old-digest'])assert.ok(html.includes(text),text);
});
test('a passed recheck does not retain an evidence repair recommendation',()=>{
 const html=render(PolicyImpactSummary,{item:{...item,action:'review_required',latest_recheck:{decision:'PASS',reason_code:'passed'}}});
 assert.doesNotMatch(html,/근거 보완 권고/);assert.match(html,/현재 근거 점검<\/span><strong>통과/);
});
test('missing materials are unavailable rather than evidence violation',()=>{
 const html=render(PolicyImpactSummary,{item:{...item,action:'unavailable',latest_recheck:{decision:'UNAVAILABLE',reason_code:'receipt_missing'}}});
 assert.match(html,/현재 근거 점검<\/span><strong>재검사 불가/);assert.doesNotMatch(html,/근거 보완 권고/);
});
test('validation examples never present mock service state as verified or maintained',()=>{
 const html=render(PolicyImpactSummary,{item:{...item,read_only:true,record_type:'validation_example',service_verified:false}});
 assert.match(html,/서비스 실행 미확인/);assert.doesNotMatch(html,/>LIVE<|서비스 유지/);
});
test('missing historical verdict is not inferred from a live service',()=>{
 const html=render(PolicyImpactSummary,{item:{...item,original_decision:null}});assert.match(html,/배포 당시 판정<\/span><strong>미보존/);
});
const diagnostics={claimed:['sqlite'],observed:'sqlite',submitted_references:['prisma/schema.prisma:7'],required_anchors:[{role:'db_provider',path:'prisma/schema.prisma',lines:[6]}],missing_roles:['db_provider']};
test('direct evidence shows submitted and required locations without claiming runtime verification',()=>{
 const html=render(EvidenceRequirements,{evidence:diagnostics});for(const term of ['schema.prisma:7','schema.prisma:6','인용 보완 필요','DB provider 선언','DB 접속 성공을 뜻하지 않습니다'])assert.ok(html.includes(term),term);
});
test('repair links original failure scope and terminal model error separately',()=>{
 const html=render(RepairHistory,{rows:[{attempt:1,target:'local',stage:'intent',status:'failed',reason:'db_provider_evidence_missing',result_code:'model_timeout',stop_reason:'model_timeout',repair_mode:'evidence_only',original_execution_id:'original-run',last_recheck_execution_id:null,policy_digest:'fixed-policy',allowed_evidence_paths:['/state/0/evidence'],metrics:{backend:'replay',model_calls:1},original_failure:{code:'db_provider_evidence_missing',diagnostics}}]});
 for(const text of ['원래 정책 차단','db_provider_evidence_missing','자동 수정 종료 사유','model_timeout','original-run','미보존','/state/0/evidence','응답 재생 · 실제 모델 호출 아님'])assert.ok(html.includes(text),text);
});
test('source-derived comparison content stays escaped',()=>{
 const html=render(PolicyChange,{change:{...change,after_condition:'<script>alert(1)</script>'}});assert.doesNotMatch(html,/<script>/);assert.match(html,/&lt;script/);
});
test('comparison request preserves the exact historical digest and default operational impacts',async()=>{
 const {api}=await import('../src/api.js');const saved=global.fetch;const paths=[];
 global.fetch=async path=>{paths.push(path);return {ok:true,json:async()=>({})};};
 try{await api.policyCompare('1.1.0','1.1.0','old/digest?');await api.policyImpacts({dataset:'operational',project:'',offset:0});}
 finally{global.fetch=saved;}
 assert.equal(paths[0],'/api/policies/1.1.0/compare?base=1.1.0&base_policy_digest=old%2Fdigest%3F');assert.match(paths[1],/dataset=operational/);
});

const reviewContext={mode:'operational_copy',copied_at:'2026-10-04T08:00:00Z'};
test('operational copy banner states isolation and snapshot time explicitly',()=>{
 const html=render(PolicyReviewContext,{context:reviewContext});
 for(const text of ['검증용 사본','재검사는 이 사본에만 기록','실시간 접속 상태를 확인하지 않습니다','새 배포·재배포를 실행할 수 없습니다','2026-10-04T08:00:00Z'])assert.ok(html.includes(text),text);
 assert.equal(render(PolicyReviewContext,{context:null}),'');
});
test('copied live status never implies current service availability',()=>{
 const html=render(PolicyImpactSummary,{item,reviewContext});
 assert.match(html,/>LIVE</);assert.match(html,/복사 당시 기록 · 현재 실행 미확인/);assert.doesNotMatch(html,/서비스 유지/);
});
test('operational copy allows recheck but offers no redeployment action',()=>{
 const copied=render(PolicyImpactActions,{item:{...item,action:'verified'},dataset:'operational',reviewContext});
 assert.match(copied,/정책 재검사/);assert.doesNotMatch(copied,/같은 소스로 재배포/);
 const ordinary=render(PolicyImpactActions,{item:{...item,action:'verified'},dataset:'operational'});
 assert.match(ordinary,/같은 소스로 재배포/);
 assert.equal(render(PolicyImpactActions,{item,dataset:'examples',reviewContext}),'');
 assert.equal(render(PolicyImpactActions,{item:{...item,read_only:true},dataset:'operational'}),'');
});
