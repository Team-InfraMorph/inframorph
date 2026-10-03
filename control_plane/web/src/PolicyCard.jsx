import {useEffect, useState} from 'react';
import {ordered, TARGETS} from './Pipeline.jsx';
const targetName = target => TARGETS[target] || target;
const inspectionKind = row => row?.purpose==='policy_update'||row?.checkpoint==='policy_update'?'정책 재검사':row?.attempt?'재시도':'최초 검사';

const labels = {PASS:'통과',BLOCK:'차단',UNSUPPORTED:'미지원',ERROR:'검사 오류',NOT_RUN:'미실행',NOT_APPLICABLE:'적용 제외'};
const stages = {source:'지원 소스',profile:'배포 프로필',intent:'분석 근거',plan:'배포 설계',patch:'코드 변경',build_profile:'빌드 입력',build:'빌드 직전',policy_update:'정책 업데이트'};
const time = value => value ? new Date(value).toLocaleString() : '시각 기록 없음';
const docLink = row => `#/policy/${row.family === 'inframorph-policy' ? row.version : 'legacy'}/rules/${row.rule_id}`;

export default function PolicyCard({data,error,onRecheck,history,onReview,onFailureReview,deploymentId}) {
  const rows=data?.results ?? [];
  const targets=ordered([...new Set(rows.map(r=>r.target))]);
  const targetSummaries=data?.summaries||[];
  const [target,setTarget]=useState('local');
  const [selected,setSelected]=useState(null);
  const [actionError,setActionError]=useState('');
  const [pending,setPending]=useState(false);
  const [classification,setClassification]=useState('unknown');
  const [summary,setSummary]=useState('');
  const [test,setTest]=useState('');
  const [fixCommit,setFixCommit]=useState('');
  const [resolved,setResolved]=useState('');
  useEffect(()=>{setSelected(null);setTarget('local');setActionError('');},[deploymentId]);
  const actualTarget=targets.includes(target)?target:targets[0];
  const list=rows.filter(r=>r.target===actualTarget);
  const latest=list.at(-1);
  const row=list.find(r=>(r.execution_id||String(r.seq))===selected) || latest;
  useEffect(()=>{setClassification('unknown');setSummary('');setTest('');setFixCommit('');setResolved('');},[deploymentId,row?.execution_id]);
  // Freeze the inspected run when new observations arrive; offer explicit navigation.
  useEffect(()=>{if (!selected && latest) setSelected(latest.execution_id||String(latest.seq));},[selected,latest]);
  async function act(fn){setPending(true);setActionError('');try{await fn();}catch(e){setActionError(e.message);}finally{setPending(false);}}
  const impact=history?.impacts?.find(x=>x.target===actualTarget);
  const job=history?.jobs?.filter(x=>x.target===actualTarget).at(-1);
  return <section className="card policy-card" id="policy-inspections" aria-label="정책 검사 결과">
    <div className="policy-heading"><div><h2>정책 검사</h2><p className="dim">정책 통과와 실제 배포·공개 접속 확인은 별도 결과입니다.</p></div><a href={`#/policy/${row?.family === "inframorph-policy" ? row.version : "1.0.0"}/overview`}>정책 문서 ↗</a></div>
    {error ? <p role="alert">정책 결과를 불러오지 못했습니다. 마지막 기록을 최신 통과 결과로 사용할 수 없습니다.</p>
      : !rows.length ? <p className="policy-empty">아직 기록된 정책 검사 결과가 없습니다.</p>
      : <>
        <div className="policy-targets" role="group" aria-label="검사 대상">{targets.map(t=><button type="button" className="secondary" aria-pressed={t===actualTarget} key={t} data-target={t} onClick={()=>{setTarget(t);setSelected(null);}}>{targetName(t)} · {targetSummaries.find(s=>s.target===t)?'배포 당시 '+(labels[targetSummaries.find(s=>s.target===t).decision]||'미완료')+' · ':''}{inspectionKind(rows.filter(r=>r.target===t&&r.purpose!=='policy_update').at(-1))}</button>)}</div>
        {row && <>
          {targetSummaries.find(s=>s.target===actualTarget)?.complete===false&&<p className="policy-warning">전체 필수 검사 기록이 완료되지 않았습니다. 아래 결과는 선택한 단계에 한정됩니다.</p>}
          <div className="policy-summary"><strong>{stages[row.stage]||'정책 검사'} · {labels[row.decision]||'알 수 없음'}</strong><span className={`policy-decision policy-${row.decision.toLowerCase()}`}>{labels[row.decision]}</span></div>
          <p>{row.family==='inframorph-policy' ? '선택한 검사 실행의 결과입니다.' : row.family==='diagnostic'?'실행 진단 · 규칙별 판정이 생성되지 않았습니다.':'기존 검사 기록 · 단계별 요약만 보존됨'}</p>
          <div className="policy-meta"><a href={row.family==='inframorph-policy'?`#/policy/${row.version}/overview`:'#/policy/legacy/overview'}>{row.family==='inframorph-policy'?'정식 정책':row.family==='diagnostic'?'검사기':'기존 정책'} {row.version}</a><span>{time(row.finished_at)}</span><span>{inspectionKind(row)} · {stages[row.checkpoint]||row.checkpoint||stages[row.stage]}</span></div>
          {row.binding?.source_revision && <p className="dim">소스 <code>{row.binding.source_revision.slice(0,12)}</code></p>}
          <label className="policy-history-label">검사 기록 <select value={row.execution_id||String(row.seq)} onChange={e=>setSelected(e.target.value)}>{list.map(r=><option key={r.seq} value={r.execution_id||String(r.seq)}>{stages[r.stage]||r.stage} · {labels[r.decision]} · {inspectionKind(r)} · {time(r.finished_at)}</option>)}</select></label>
          {latest!==row && <button className="secondary" onClick={()=>setSelected(latest.execution_id||String(latest.seq))}>새 검사 결과 보기</button>}
          {(!row.complete || ['ERROR','UNSUPPORTED'].includes(row.decision)) && <p className="policy-warning">검사 미완료 · 확인하지 못한 항목을 통과로 해석하지 마세요.</p>}
          <details className="policy-detail" key={row.execution_id||row.seq} open={row.decision!=='PASS'}><summary>검사 상세 {row.rules ? `· 수행 ${row.evaluated_rules?.length||0} / 필수 ${row.required_rules?.length||0}` : ''}</summary>
            <ul className="policy-results">{(row.rules||[row]).map((r,i)=><li key={r.rule_id+i}>
              <details open={['BLOCK','ERROR','UNSUPPORTED'].includes(r.decision)}>
                <summary><span>{r.title}</span><span className={`policy-decision policy-${r.decision.toLowerCase()}`}>{labels[r.decision]||r.decision}</span></summary>
                {r.expected&&<p><strong>기대 조건</strong> {r.expected}</p>}
                {r.evidence && Object.keys(r.evidence).length>0 && <div><strong>관찰한 근거</strong><pre>{Object.entries(r.evidence).map(([key,value])=>`${({references:'근거 파일·줄',path:'파일',observed:'관찰값',claimed:'분석값',setting_names:'일반 설정 이름',secret_count:'비밀정보 이름 수',before:'변경 전',after:'변경 후',file_count:'검사 파일 수',changed_paths:'변경 파일',target:'배포 대상',source_revision:'소스 commit',services:'서비스',public_services:'공개 서비스',input_digest:'입력 해시'})[key]||key}: ${Array.isArray(value)?value.join(', '):typeof value==='object'?JSON.stringify(value):String(value)}`).join('\n')}</pre></div>}
                {r.path&&<p><code>{r.path}{r.line?`:${r.line}`:''}</code></p>}
                {r.decision!=='PASS'&&r.remedy&&<p>다음 조치: {r.remedy}</p>}
                <p><a href={docLink({...row,rule_id:r.rule_id})}>{r.rule_id} · 규칙 설명 ↗</a>{row.family==='inframorph-policy'&&<> · <a href={docLink({...row,rule_id:r.rule_id})+'?section='+(['BLOCK','ERROR','UNSUPPORTED'].includes(r.decision)?'remedy':'procedure')}>{['BLOCK','ERROR','UNSUPPORTED'].includes(r.decision)?'해결과 재검증':'처리 과정'} ↗</a></>}</p>
                <small className="dim">{r.reason_code}</small>
              </details>
            </li>)}</ul>
            <details><summary>검사 식별 정보</summary><pre>{JSON.stringify({execution_id:row.execution_id,policy_digest:row.policy_digest,checkpoint:row.checkpoint,binding:row.binding},null,2)}</pre></details>
          </details>
          {row.decision!=='PASS' && row.execution_id && onFailureReview && <details className="policy-detail"><summary>실패 원인 검토와 수정 연결</summary><p>원래 판정은 유지합니다. 이 검토로 실행을 허용하거나 정책을 완화하지 않습니다.</p>
            <form onSubmit={e=>{e.preventDefault();act(()=>onFailureReview(row.execution_id,{classification,summary,regression_test:test,fix_commit:fixCommit,resolved_execution_id:resolved}));}}>
              <label>원인 분류<select value={classification} onChange={e=>setClassification(e.target.value)}>{Object.entries({unknown:'확인 불가',violation:'정상 위반 차단',false_positive:'확인된 오탐',unsupported:'미지원',validator_error:'검사기 오류',environment_error:'배포 환경 오류'}).map(([k,v])=><option key={k} value={k}>{v}</option>)}</select></label>
              <label>검토 근거<textarea maxLength={2000} value={summary} onChange={e=>setSummary(e.target.value)}/></label>
              <label>회귀 테스트<input value={test} onChange={e=>setTest(e.target.value)} placeholder="tests/test_policy_lifecycle.py::test_case"/></label>
              <label>수정 commit<input value={fixCommit} onChange={e=>setFixCommit(e.target.value)} placeholder="40자리 commit (선택)"/></label>
              <label>해결 검사 ID<input value={resolved} onChange={e=>setResolved(e.target.value)} placeholder="통과한 검사 ID (선택)"/></label>
              <button disabled={pending}>검토 기록 저장</button>
            </form>
          </details>}
        </>}
      </>}
    {impact && <aside className="policy-impact"><strong>현재 정책 변경 영향</strong><p>{impact.impact.reason==='target_scope_requires_review'?'온프레미스는 Local에서 검증한 이미지를 사용합니다. 대상별 정책 재검사 자료는 아직 연결되지 않았습니다.':impact.impact.reason==='current_policy'?'현재 활성 정책으로 검사한 배포입니다.':impact.impact.review?'새 정책으로 재검사하고 배포별 검토가 필요합니다.':'변경 영향을 확인하세요.'}</p><a href={`#/policy/${impact?.active?.version || row?.version || "1.0.0"}/impacts`}>배포 영향과 필요한 조치 ↗</a>
      {onRecheck&&<button className="secondary" disabled={pending||error} onClick={()=>act(()=>onRecheck(actualTarget))}>정책 재검사</button>}
      {job?.result && <p>최근 재검사: {labels[job.result.decision]||'재검사 불가'} · {job.result.reason_code}</p>}
      {job?.result?.decision==='PASS'&&!job.reviewed&&<a href={`#/policy/${impact?.active?.version || row?.version || "1.0.0"}/impacts`}>변경 내용을 확인하고 검토하기</a>}
    </aside>}
    <PolicyHistory key={deploymentId} results={rows} history={history} target={actualTarget} selectable={rows.map(r=>r.execution_id)} onSelect={id=>{const found=rows.find(r=>r.execution_id===id);if(found){setTarget(found.target);setSelected(id);}}}/>
    {actionError&&<p role="alert">{actionError}</p>}
  </section>;
}


const eventLabels={policy_impact_assessed:'정책 변경 영향 평가',operational_failure:'실행 실패 진단',inspection_started:'검사 시작',inspection_finished:'검사 종료',inspection_interrupted:'검사 중단 · 결과 없음',rule_evaluated:'규칙 판정',recheck_requested:'재검사 요청',recheck_finished:'재검사 종료',policy_reviewed:'정책 변경 검토 완료',failure_reviewed:'실패 원인 검토',policy_redeploy_requested:'후속 배포 요청',policy_redeploy_finished:'후속 배포 종료',deployment_inputs_preserved:'배포 입력 보존',diagnostic_unavailable:'상세 로그 저장 실패'};
const causeLabels={unknown:'확인 불가',violation:'정상 위반 차단',false_positive:'확인된 오탐',unsupported:'미지원',validator_error:'검사기 오류',environment_error:'배포 환경 오류'};
const problem = decision => ['BLOCK','ERROR','UNSUPPORTED','NOT_RUN'].includes(decision);
const eventDecision = e => e.payload?.decision || (e.event==='inspection_interrupted'?'ERROR':null);
export function historyGroups(events,results=[]) {
  const groups=new Map();
  for(const e of [...events].sort((a,b)=>a.seq-b.seq)) {
    const key=e.execution_id || `event-${e.seq}`;
    if(!groups.has(key))groups.set(key,{key,executionId:e.execution_id,events:[]});
    groups.get(key).events.push(e);
  }
  return [...groups.values()].map(g=>{
    const result=results.find(r=>r.execution_id===g.executionId);
    const finished=g.events.findLast(e=>e.event==='inspection_finished');
    const interrupted=g.events.some(e=>e.event==='inspection_interrupted');
    const checkpoint=result?.stage || g.events.find(e=>e.payload?.checkpoint)?.payload.checkpoint;
    return {...g,result,title:stages[checkpoint]||checkpoint||(g.executionId?'검사 실행':'운영 기록'),
      decision:interrupted?'ERROR':result?.decision||finished?.payload?.decision||null,
      pending:!result&&!finished&&!interrupted&&g.events.some(e=>e.event==='inspection_started')};
  });
}
function HistoryAttachments({history,executionId}) {
  const diagnostics=(history.diagnostics||[]).filter(d=>(d.execution_id||null)===(executionId||null));
  const reviews=(history.failure_reviews||[]).filter(r=>(r.execution_id||null)===(executionId||null));
  return <>{diagnostics.map(d=><details className="history-attachment" key={d.id}><summary>진단 로그 · {d.payload?'마스킹됨':'상세 로그 보존 기간 만료'}</summary>{d.payload&&<><pre>{d.payload.text}</pre><small>{d.payload.masked?'민감정보 제거됨 · ':''}{d.payload.truncated?'일부 생략됨':''}</small></>}</details>)}
    {reviews.map(r=><article className="history-review" key={r.id}><strong>원인 검토 · {causeLabels[r.payload.classification]||'확인 불가'}</strong><p>{r.payload.summary}</p><dl><dt>수정 commit</dt><dd>{r.payload.fix_commit||'수정 기록 없음'}</dd><dt>회귀 테스트</dt><dd>{r.payload.regression_test||'연결 전'}</dd><dt>해결 검사</dt><dd>{r.payload.resolved_execution_id||'미해결'}</dd></dl></article>)}</>;
}
export function PolicyHistory({history,target,onSelect,selectable=[],results=[]}) {
  const [message,setMessage]=useState('');
  const [view,setView]=useState('groups');
  const [filter,setFilter]=useState('all');
  const [query,setQuery]=useState('');
  const [seen,setSeen]=useState(null);
  useEffect(()=>{setSeen(null);},[target]);
  useEffect(()=>{if(seen===null && history)setSeen((history.events||[]).filter(e=>!target||!e.target||e.target===target).length);},[history,target,seen]);
  const events=(history?.events||[]).filter(e=>!target||!e.target||e.target===target);
  const groups=historyGroups(events,results);
  const matches=e=>!query.trim() || [eventLabels[e.event],e.execution_id,e.payload?.rule_id,e.payload?.reason_code,labels[eventDecision(e)],e.payload?.checkpoint].filter(Boolean).join(' ').toLowerCase().includes(query.trim().toLowerCase());
  const inFilter=decision=>filter==='all'||(filter==='problems'?problem(decision):decision===filter);
  const shown=groups.filter(g=>(g.decision?inFilter(g.decision):filter==='all'||g.events.some(e=>inFilter(eventDecision(e))))&&(g.events.some(matches)||g.title.toLowerCase().includes(query.toLowerCase())));
  const logs=events.filter(e=>inFilter(eventDecision(e))&&matches(e));
  if(!history)return null;
  const orphanIds=[...new Set([...(history.diagnostics||[]),...(history.failure_reviews||[])].map(x=>x.execution_id||null))].filter(id=>!groups.some(g=>(g.executionId||null)===id)&&(!id||!results.some(r=>r.execution_id===id&&target&&r.target!==target)));
  const exportSummary=async()=>{try{const ids=new Set(events.map(e=>e.execution_id));await navigator.clipboard.writeText(JSON.stringify({events,diagnostics:(history.diagnostics||[]).filter(d=>ids.has(d.execution_id)),failure_reviews:(history.failure_reviews||[]).filter(r=>ids.has(r.execution_id))},null,2));setMessage('마스킹된 재현 요약을 복사했습니다.');}catch{setMessage('복사를 사용할 수 없습니다. 기록에서 필요한 내용을 선택하세요.');}};
  const renderEvents=list=><div className="history-log" role="region" aria-label="정책 이벤트 로그" tabIndex={0}><table><thead><tr><th>시각</th><th>종류</th><th>규칙</th><th>결과</th><th>설명·연결</th></tr></thead><tbody>{list.map(e=><tr key={e.seq} className={problem(eventDecision(e))?'history-problem':''}>
    <td><time title={time(e.occurred_at)}>{e.occurred_at?new Date(e.occurred_at).toLocaleTimeString():'시각 없음'}</time><small>#{e.seq}</small></td><td>{eventLabels[e.event]||'정책 운영 이벤트'}</td><td><code>{e.payload?.rule_id||'—'}</code></td><td>{labels[eventDecision(e)]||'—'}</td>
    <td>{e.payload?.reason_code==='passed'?'검사 조건 충족':e.payload?.reason_code||stages[e.payload?.checkpoint]||'—'}{e.payload?.new_deployment_id&&<p>후속 배포 <code>{e.payload.new_deployment_id}</code></p>}<details><summary>연결 정보</summary><code>{e.execution_id||e.payload?.policy_digest||'실행 연결 없음'}</code>{onSelect&&selectable.includes(e.execution_id)&&<button className="secondary" onClick={()=>onSelect(e.execution_id)}>이 검사 보기</button>}</details></td>
  </tr>)}</tbody></table></div>;
  return <section className="policy-history" aria-label="검사 이력">
    <div className="history-heading"><div><h3>검사 이력</h3><p className="dim">{target?targetName(target):'전체 대상'} · 검사 {groups.filter(g=>g.executionId).length}회 · 이벤트 {events.length}건</p></div><span className="dim">시간순 · 새 기록은 아래에 추가됩니다</span></div>
    {seen!==null&&events.length>seen&&<button className="secondary" onClick={()=>setSeen(events.length)}>새 기록 {events.length-seen}건 · 목록에 추가됨</button>}
    <div className="history-toolbar"><div role="group" aria-label="이력 보기 방식"><button className="secondary" aria-pressed={view==='groups'} onClick={()=>setView('groups')}>검사별 보기</button><button className="secondary" aria-pressed={view==='logs'} onClick={()=>setView('logs')}>전체 로그</button></div>
      <label>상태<select value={filter} onChange={e=>setFilter(e.target.value)}><option value="all">전체 상태</option><option value="problems">차단·오류·미지원·미실행</option>{Object.entries(labels).map(([value,label])=><option key={value} value={value}>{label}</option>)}</select></label>
      <label>검색<input type="search" placeholder="규칙·사유·검사 검색" value={query} onChange={e=>setQuery(e.target.value)}/></label>
    </div>
    {!events.length&&<p className="policy-empty">아직 연결된 조치 기록이 없습니다.</p>}
    {events.length>0&&(view==='groups'?!shown.length:!logs.length)&&<p role="status">조건에 맞는 기록이 없습니다. 상태 또는 검색어를 변경해 주세요.</p>}
    {view==='logs'&&logs.length>0&&renderEvents(logs)}
    {view==='groups'&&<div className="history-runs">{shown.map(g=><details key={g.key} className={`history-run ${problem(g.decision)?'history-problem':''}`} open={problem(g.decision)?true:undefined}>
      <summary><time>{time(g.events[0].occurred_at)}</time><strong>{g.title}</strong><span className={`history-status ${g.decision==='PASS'?'history-pass':''}`}>{labels[g.decision]||(g.pending?'검사 진행 · 종료 기록 없음':'판정 기록 없음')}</span><span className="dim">이벤트 {g.events.length}건</span></summary>
      <div className="history-run-body">{g.result&&<p>{g.result.family==='inframorph-policy'?'정식 정책':'기존 검사 기록'} {g.result.version} · {inspectionKind(g.result)}{g.result.checkpoint?` · ${g.result.checkpoint}`:''}</p>}
        {onSelect&&selectable.includes(g.executionId)&&<button className="secondary" onClick={()=>onSelect(g.executionId)}>규칙 결과 보기</button>}
        {renderEvents(g.events)}<HistoryAttachments history={history} executionId={g.executionId}/>
        {problem(g.decision)&&!(history.failure_reviews||[]).some(r=>r.execution_id===g.executionId)&&<p className="dim">원인 검토·수정 기록 없음 · 아직 정책 결함이나 오탐으로 분류되지 않았습니다.</p>}
      </div></details>)}</div>}
    {view==='logs'&&<details className="history-attachment"><summary>진단·수정 연결 보기</summary>{groups.filter(g=>g.executionId).map(g=><div key={g.key}><HistoryAttachments history={history} executionId={g.executionId}/></div>)}</details>}
    {orphanIds.map(id=><details key={id||'unlinked'} className="history-attachment" open><summary>진단·수정 기록 · 검사 이벤트 연결 없음</summary>{id&&<code>{id}</code>}<HistoryAttachments history={history} executionId={id}/></details>)}
    <div className="history-footer"><button type="button" className="secondary" onClick={exportSummary}>마스킹된 재현 요약 복사</button><span role="status">{message}</span></div>
  </section>;
}
