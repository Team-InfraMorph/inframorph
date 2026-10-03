const decision = value => ({PASS:'통과',BLOCK:'차단',UNAVAILABLE:'재검사 불가',ERROR:'검사 오류',UNSUPPORTED:'미지원'})[value] || value || '미보존';

export function PolicyChange({change,version='1.1.0'}) {
  const title=change.title||(change.id==='bounded-policy-repair'?'기존 변경 · 제한적 AI 자동 수정':change.reason);
  return <article className="policy-detail policy-change">
    <h3>{title}</h3>
    {title!==change.reason&&<p>{change.reason}</p>}
    {(change.before_condition||change.after_condition)&&<div className="policy-condition-comparison">
      <div><strong>이전 조건</strong><p>{change.before_condition||'이전 조건 미보존 · 확인 필요'}</p></div>
      <div><strong>새 조건</strong><p>{change.after_condition||'새 조건 확인 필요'}</p></div>
    </div>}
    {change.applies&&<p><strong>해당하는 배포</strong> {change.applies}</p>}
    {change.applicability==='unknown'&&<p className="policy-warning">보존 자료가 부족해 이 변경의 적용 여부를 확인해야 합니다.</p>}
    {(change.examples||[]).map(example=><p className="policy-change-example" key={example.id}><strong>{decision(example.before_decision)} → {decision(example.after_decision)}</strong> · {example.description}</p>)}
    {!!change.actions?.length&&<div><strong>필요한 조치</strong><ul>{change.actions.map((action,i)=><li key={i}>{action}</li>)}</ul></div>}
    <p className="dim">재검사 {change.recheck?'필요':'요구 없음'} · 검토 {change.review?'필요':'요구 없음'} · 재배포 {change.redeploy?'필요':'요구 없음'}</p>
    <p>{(change.rule_ids||[]).map(id=><a key={id} className="policy-rule-link" href={`#/policy/${version}/rules/${id}?section=decisions`}>{id} · 판정 조건 ↗</a>)}</p>
    <details><summary>구현·검증 연결</summary><p>회귀 근거: {(change.regression_tests||[]).join(', ')||'연결된 검증 근거 없음'}</p><p>실패 사례: {(change.failure_references||[]).join(', ')||'연결된 사례 없음'}</p></details>
  </article>;
}

export function PolicyImpactSummary({item}) {
  const historical = item.policy?.decision || item.original_decision;
  const recheck = item.latest_recheck || item.recheck_result || item.job?.result;
  const example = item.read_only || item.record_type==='validation_example';
  const advisory = item.action==='evidence_confirmation_required'||(item.impact?.advisory&&recheck?.decision==='BLOCK'&&['db_provider_evidence_missing','worker_command_evidence_missing','worker_start_evidence_missing'].includes(recheck.reason_code));
  return <>
    <div className="policy-impact-states">
      <div><span>배포 당시 판정</span><strong>{decision(historical)}</strong><small>정책 {item.policy?.version||'미보존'} · 원래 기록 유지</small></div>
      <div><span>현재 근거 점검</span><strong>{advisory?'근거 보완 권고':recheck?decision(recheck.decision):'재검사 결과 확인 필요'}</strong><small>{recheck?.reason_code||item.impact?.reason||'세부 사유 미보존'}</small></div>
      <div><span>서비스 상태</span><strong>{example?'서비스 실행 미확인':item.status||'미보존'}</strong><small>{example?'정책 검증 예시':'정책 변경만으로 중단·재배포하지 않음'}</small></div>
    </div>
    {advisory&&<p className="policy-warning">{example?'근거 보완 권고 · 서비스 실행 미확인.':'근거 보완 권고 · 서비스 유지.'} 새 기준의 판정은 당시 기록과 별도로 보존합니다.</p>}
    <p>배포 당시 정책 {item.policy?.version||'미보존'} → 현재 활성 정책 {item.active?.version||'확인 필요'}</p>
    {item.policy?.policy_digest&&<a href={`#/policy/${item.active?.version||'1.1.0'}/compare?base=${encodeURIComponent(item.policy.version)}&base_policy_digest=${encodeURIComponent(item.policy.policy_digest)}`}>당시 검사에서 달라진 조건 보기 ↗</a>}
  </>;
}
