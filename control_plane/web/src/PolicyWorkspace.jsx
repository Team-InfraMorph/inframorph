import {TARGETS} from './Pipeline.jsx';
import {useEffect,useState,useRef} from 'react';
import {api} from './api.js';
import PolicyCard from './PolicyCard.jsx';
import PolicyDocument, {documentHeadings,documentGroup} from './PolicyDocument.jsx';

const actionLabels={inspection_incomplete:'검사 기록 미완료',current:'현재 정책',recheck_required:'재검사 필요',queued:'재검사 대기',running:'검사 중',review_required:'검토 필요',redeploy_required:'재배포 필요',verified:'재검사 확인',unavailable:'재검사 불가',action_required:'수정·확인 필요',finished:'검사 종료'};
const title=d=>d.body.split('\n')[0].replace(/^# /,'');
const anchor=text=>encodeURIComponent(text.trim());
function DeploymentEvidence({item}) {
  const [history,setHistory]=useState(null),[data,setData]=useState(null),[error,setError]=useState(false),[expanded,setExpanded]=useState(false);
  const refresh=()=>Promise.all([api.policyHistory(item.id),api.policy(item.id)]).then(([h,d])=>{setHistory(h);setData({...d,results:d.results.filter(r=>r.target===item.target)});setError(false);}).catch(()=>setError(true));
  useEffect(()=>{if(!expanded)return;refresh();const t=setInterval(refresh,5000);return()=>clearInterval(t);},[expanded,item.id,item.target]);
  return <details onToggle={e=>setExpanded(e.currentTarget.open)}><summary>당시 검사·실패·수정 기록 보기</summary>{expanded&&<PolicyCard data={data} history={history} error={error} deploymentId={item.id} onFailureReview={(id,body)=>api.policyFailureReview(item.id,id,body).then(refresh)}/>}</details>;
}

function ConfirmDialog({confirm,busy,onCancel,onConfirm}) {
  const ref=useRef(null);
  useEffect(()=>{ref.current?.showModal();return()=>ref.current?.close();},[]);
  return <dialog ref={ref} className="policy-modal" aria-labelledby="policy-confirm-title" onCancel={onCancel}>
    <div className="card"><h2 id="policy-confirm-title">{confirm.kind==='review'?'정책 변경 검토':'동일 소스 재배포'}</h2><p>{confirm.item.project_id} · {TARGETS[confirm.item.target]||confirm.item.target}</p><p>소스 {confirm.item.commit_sha}</p><p>정책 {confirm.item.active.version} · {confirm.item.active.policy_digest.slice(0,12)}</p><ul>{confirm.item.impact.changes.map(c=><li key={c.id}>{c.reason}</li>)}</ul><p>{confirm.kind==='review'?'이 확인은 위반을 면제하지 않습니다. 재검사한 입력과 정책이 바뀌면 다시 검토합니다.':'프로젝트의 배포 대상 전체에 새 배포를 시작합니다. 기존 배포 기록은 유지됩니다.'}</p><div className="row"><button autoFocus className="secondary" disabled={busy} onClick={onCancel}>취소</button><button disabled={busy} onClick={onConfirm}>{confirm.kind==='review'?'검토 완료 기록':'재배포 시작'}</button></div></div>
  </dialog>;
}


export default function PolicyWorkspace({route}) {
  const [path,search='']=route.split('?');
  const params=new URLSearchParams(search),revision=params.get('revision');
  const [, , version='1.0.0',...segments]=path.split('/');
  const slug=segments.join('/')||'overview';
  const [catalog,setCatalog]=useState(null),[page,setPage]=useState(null),[error,setError]=useState(''),[query,setQuery]=useState(''),[mobileNav,setMobileNav]=useState(false);
  const [base,setBase]=useState('legacy'),[diff,setDiff]=useState(null),[items,setItems]=useState([]),[project,setProject]=useState(''),[target,setTarget]=useState(''),[status,setStatus]=useState(''),[policyVersion,setPolicyVersion]=useState(''),[reason,setReason]=useState('');
  const returnFocus=useRef(null);
  const closeConfirm=()=>{setConfirm(null);requestAnimationFrame(()=>returnFocus.current?.focus());};
  const [confirm,setConfirm]=useState(null),[busy,setBusy]=useState(false),[notice,setNotice]=useState(''),[loading,setLoading]=useState(true);
  const [offset,setOffset]=useState(0),[total,setTotal]=useState(0),[stats,setStats]=useState([]);
  useEffect(()=>{if(page&&params.get('section'))requestAnimationFrame(()=>{const element=document.getElementById(params.get('section'))||document.getElementById(anchor(params.get('section')));const details=element?.closest('details');if(details)details.open=true;element?.scrollIntoView();});},[route,page]);
  useEffect(()=>{api.policies().then(setCatalog).catch(e=>setError(e.message));},[]);
  useEffect(()=>{let alive=true;setError('');setPage(null);if(version!=='legacy')api.policyVersion(version,revision).then(v=>alive&&setPage(v)).catch(e=>alive&&setError(e.message));return()=>{alive=false;};},[version,revision]);
  useEffect(()=>{let alive=true;setDiff(null);setError('');if(slug==='compare'&&version!=='legacy')api.policyCompare(version,base).then(v=>alive&&setDiff(v)).catch(e=>alive&&setError(e.message));return()=>{alive=false;};},[slug,version,base]);
  const refresh=()=>api.policyImpacts({project,target,action:status,policy_version:policyVersion,reason,offset}).then(v=>{setItems(v.items);setTotal(v.total);});
  useEffect(()=>{if(slug!=='impacts')return;let alive=true;setLoading(true);setItems([]);const fetch=()=>api.policyImpacts({project,target,action:status,policy_version:policyVersion,reason,offset}).then(v=>{if(alive){setItems(v.items);setTotal(v.total);setLoading(false);setError('');}}).catch(e=>{if(alive){setError(e.message);setLoading(false);}});fetch();const t=setInterval(fetch,5000);return()=>{alive=false;clearInterval(t);};},[slug,project,target,status,policyVersion,reason,offset]);
  useEffect(()=>{if(slug==='impacts')api.policyStatistics().then(v=>setStats(v.items)).catch(()=>setError('규칙별 집계를 불러오지 못했습니다.'));},[slug,items]);
  async function act(fn){setBusy(true);setError('');try{const result=await fn();setNotice(result?.deployment_id?`후속 배포를 생성했습니다: ${result.deployment_id}`:'작업을 기록했습니다.');await refresh();}catch(e){setError(e.message);}finally{setBusy(false);closeConfirm();}}
  const prefix=`#/policy/${version}/`;
  const currentRevision=Number(revision||page?.release.document_revision||1);
  const docHref=slug=>prefix+slug+'?revision='+currentRevision;
  const doc=page?.documents?.find(d=>d.slug===slug);
  const docs=page?.documents||[];
  const results=query?docs.filter(d=>(title(d)+' '+d.body).toLowerCase().includes(query.toLowerCase())):docs;
  const groups=['정책 이해','실행과 문제 해결','규칙별 설명','사례와 검증','변경과 보완'];
  const newestRevision=Math.max(...(page?.document_revisions||[currentRevision]));
  return <main className="policy-workspace">
    <div className="policy-heading"><div><h1>정책</h1><p className="dim">규칙을 이해하고, 변경을 비교하고, 배포에 필요한 조치를 확인합니다.</p></div><a href="#/deploy">배포 화면으로 돌아가기</a></div>
    <nav className="policy-tabs" aria-label="정책 영역"><a aria-current={!['compare','impacts'].includes(slug)?'page':undefined} href={prefix+'overview'}>정책 문서</a><a aria-current={slug==='compare'?'page':undefined} href={prefix+'compare'}>버전·변경 비교</a><a aria-current={slug==='impacts'?'page':undefined} href={prefix+'impacts'}>배포 영향</a></nav>
    <div className="policy-version-bar"><label>문서 버전 <select value={version} onChange={e=>{location.hash=`/policy/${e.target.value}/${slug}`+(params.get('section')?'?section='+anchor(params.get('section')):'');}}>{(catalog?.releases||[]).map(r=><option key={r.version} value={r.version}>{r.version}</option>)}<option value="legacy">기존 검사 기록 · 2.2.0</option></select></label>{page&&<label>문서 revision <select value={revision||page.release.document_revision} onChange={e=>{location.hash=`/policy/${version}/${slug}?revision=${e.target.value}`;}}>{page.document_revisions?.map(r=><option key={r}>{r}</option>)}</select></label>}<span>현재 활성 정책 {catalog?.active?.version||'확인 중'}</span><small>문서 선택은 실행 정책을 바꾸지 않습니다.</small></div>
    {error&&<p role="alert" className="error banner">{error}</p>}{notice&&<p role="status">{notice}</p>}
    {slug==='impacts'?<section className="card"><h2>운영 배포의 정책 상태</h2><p>서비스 상태와 정책 조치는 별개입니다. 자동으로 서비스를 중단하거나 재배포하지 않습니다.</p>
      <details className="policy-detail"><summary>규칙별 실패와 검토 현황</summary><p>실제로 수행한 해당 규칙 검사 수가 분모입니다. 공격 차단 확률이 아닙니다. 배포 검사와 정책 업데이트 검사는 따로 집계합니다.</p><div className="policy-table-wrap"><table><thead><tr><th>규칙·정책·용도</th><th>실행</th><th>차단</th><th>오류</th><th>미지원</th><th>미실행</th><th>확인된 오탐</th><th>미해결</th></tr></thead><tbody>{stats.map(r=><tr key={r.rule_id+r.version+r.purpose}><td>{r.rule_id} · {r.version} · {r.purpose==='policy_update'?'재검사':'배포'}</td><td>{r.performed}</td><td>{r.blocked}/{r.performed}</td><td>{r.errors}</td><td>{r.unsupported}</td><td>{r.not_run}</td><td>{r.false_positive}</td><td>{r.unresolved}</td></tr>)}</tbody></table></div></details><div className="policy-filters"><label>당시 정책<input value={policyVersion} onChange={e=>{setPolicyVersion(e.target.value);setOffset(0);}} placeholder="legacy/2.2.0"/></label><label>실패 사유 코드<input value={reason} onChange={e=>{setReason(e.target.value);setOffset(0);}} placeholder="db_provider_mismatch"/></label><label>프로젝트 ID<input value={project} onChange={e=>{setProject(e.target.value);setOffset(0);}}/></label><label>대상<select value={target} onChange={e=>{setTarget(e.target.value);setOffset(0);}}><option value="">전체</option><option value="local">Local 테스트</option><option value="onprem">온프레미스</option><option value="aws">AWS</option></select></label><label>필요 조치<select value={status} onChange={e=>{setStatus(e.target.value);setOffset(0);}}><option value="">전체</option>{Object.entries(actionLabels).map(([k,v])=><option value={k} key={k}>{v}</option>)}</select></label></div>
      {loading?<p role="status">배포 영향을 불러오는 중입니다.</p>:!items.length?<p>조건에 맞는 운영 배포가 없습니다.</p>:items.map(item=><details className="policy-deployment card" key={item.id+item.target}><summary><strong>{item.project_id} · {TARGETS[item.target]||item.target}</strong><span>{actionLabels[item.action]}</span></summary><p>서비스 {item.status} · 소스 <code>{item.commit_sha}</code></p><p>배포 당시 {item.policy.family==='legacy'?'기존 기록 ':''}{item.policy.version} → 활성 정책 {item.active.version}</p><p>재검사 {item.impact.recheck?'필요':'요구 없음'} · 운영자 검토 {item.impact.review?'필요':'요구 없음'} · 재배포 {item.impact.redeploy?'필요':'요구 없음'}</p>
        <ul>{item.impact.changes.map(c=><li key={c.id}>{c.reason}</li>)}</ul><a href={prefix+'upgrade'}>업그레이드 안내 ↗</a>
        <div className="row"><button className="secondary" disabled={busy||!!error||item.action==='running'} onClick={()=>act(()=>api.policyRecheck(item.id,item.target))}>정책 재검사</button>{item.action==='review_required'&&<button disabled={busy} onClick={e=>{returnFocus.current=e.currentTarget;setConfirm({kind:'review',item});}}>검토 내용 확인</button>}<button className="secondary" disabled={busy||!!error||!['current','verified','redeploy_required'].includes(item.action)} onClick={e=>{returnFocus.current=e.currentTarget;setConfirm({kind:'redeploy',item});}}>같은 소스로 재배포</button></div>
        <DeploymentEvidence item={item}/>
      </details>)}
      <div className="row"><button disabled={!offset} onClick={()=>setOffset(Math.max(0,offset-50))}>이전</button><span>{total}개 중 {total?offset+1:0}–{Math.min(offset+50,total)}</span><button disabled={offset+50>=total} onClick={()=>setOffset(offset+50)}>다음</button></div>
    </section>:version==='legacy'?<article className="card"><h2>기존 검사 기록 · 2.2.0</h2><p>정식 관리 도입 이전의 단계별 요약 기록입니다. 새 정책 1.0.0과 숫자로 비교하지 않습니다.</p><p>세부 규칙·시각·구현 해시가 없는 기록은 복원한 것으로 표시하지 않습니다. 현재 선택한 규칙의 당시 상세 자료는 제공되지 않습니다.</p><a href="#/policy/1.0.0/upgrade">1.0.0 도입 전환 안내</a></article>:slug==='compare'?<section className="card"><h2>버전·변경 비교</h2><label>기준 버전<select value={base} onChange={e=>setBase(e.target.value)}><option value="legacy">기존 검사 기록 · 2.2.0</option>{catalog?.releases?.map(r=><option key={r.version}>{r.version}</option>)}</select></label><p>목표 정책 {version}. 중간 릴리스의 필수 변경도 포함합니다.</p>{diff&&<><p>추가 {diff.added.length} · 변경 {diff.changed.length} · 제거 {diff.removed.length}</p>{diff.changes.map(c=><article key={c.id} className="policy-detail"><h3>{c.reason}</h3><p>재검사 {c.recheck?'필요':'요구 없음'} · 검토 {c.review?'필요':'요구 없음'} · 재배포 {c.redeploy?'필요':'요구 없음'}</p><p>{c.rule_ids.join(' · ')}</p><p>회귀 테스트: {(c.regression_tests||[]).join(', ')||'변경 명세에서 연결 필요'}</p><p>관련 실패 사례: {(c.failure_references||[]).join(', ')||'등록된 사례 없음'}</p></article>)}</>}</section>:
      <div className="policy-doc-layout"><aside className="policy-sidebar"><label>이 버전에서 검색<input type="search" value={query} onChange={e=>setQuery(e.target.value)} placeholder="규칙 ID 또는 검색어"/></label><button className="policy-mobile-nav secondary" aria-expanded={mobileNav||!!query} onClick={()=>setMobileNav(!mobileNav)}>문서 목차 {mobileNav?'접기':'펼치기'}</button><nav className={mobileNav||query?'is-expanded':''} aria-label="정책 문서 목차">{groups.map(group=>{const entries=results.filter(d=>documentGroup(d.slug)===group);return entries.length?<div key={group}><h3>{group}</h3>{entries.map(d=><a key={d.slug} aria-current={d.slug===slug?'page':undefined} href={docHref(d.slug)}>{title(d)}</a>)}</div>:null;})}{!results.length&&<p role="status">이 문서 버전에 검색 결과가 없습니다.</p>}</nav></aside><article className="card policy-document">{doc?<><div className="policy-doc-context"><span>정책 {version} · 문서 revision {doc.revision}</span><a href={docHref(slug)}>이 문서 고정 링크</a></div>{currentRevision<newestRevision&&<aside className="policy-doc-notice">당시 설명 revision {currentRevision}을 보고 있습니다. <a href={prefix+slug+'?revision='+newestRevision}>보완된 설명 revision {newestRevision} 보기</a></aside>}<details className="policy-mobile-toc"><summary>이 문서에서 찾기</summary><nav aria-label="본문 목차">{documentHeadings(doc.body).filter(h=>h.level===2).map(h=><a key={h.id} href={docHref(slug)+'&section='+encodeURIComponent(h.id)}>{h.title}</a>)}</nav></details><PolicyDocument body={doc.body} version={version} revision={doc.revision} slug={slug}/><footer>문서 revision {doc.revision} · <code>{doc.sha256.slice(0,12)}</code></footer></>:<p>{page?'이 버전에는 해당 문서·규칙이 없습니다.':'문서를 불러오는 중입니다.'}</p>}</article><aside className="policy-toc"><strong>이 문서에서</strong>{doc&&documentHeadings(doc.body).filter(h=>h.level===2).map(h=><a key={h.id} href={docHref(slug)+'&section='+encodeURIComponent(h.id)}>{h.title}</a>)}</aside></div>}
    {confirm&&<ConfirmDialog confirm={confirm} busy={busy} onCancel={closeConfirm} onConfirm={()=>act(()=>confirm.kind==='review'?api.policyReview(confirm.item.id,confirm.item.job_id,confirm.item.active.policy_digest):api.policyRedeploy(confirm.item.id,confirm.item.commit_sha,confirm.item.active.policy_digest))}/>}

  </main>;
}
