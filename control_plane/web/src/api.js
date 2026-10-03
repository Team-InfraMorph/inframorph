async function request(method, path, body) {
  const res = await fetch(`/api${path}`, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join(", ") : data.detail;
    const policyErrors={stale_policy_redeploy:'정책 또는 소스가 바뀌었습니다. 새 내용을 확인하세요.',stale_or_failed_policy_review:'통과한 최신 재검사 결과가 필요합니다.',policy_review_required:'프로젝트의 모든 배포 대상에서 필요한 정책 검토를 완료하세요.',policy_redeploy_requires_runtime:'현재 서버는 실제 재배포 실행이 설정되지 않았습니다.',false_positive_requires_evidence:'오탐 검토 근거와 회귀 테스트를 함께 기록하세요.',resolved_execution_not_passed:'같은 프로젝트·검사 단계의 해당 규칙을 통과한 결과를 연결하세요.',policy_history_incomplete:'버전 변경 이력이 부족해 영향을 확정할 수 없습니다.',unknown_policy_document:'선택한 버전에는 해당 문서가 없습니다.'};
    throw new Error(policyErrors[detail] || detail || `${res.status} 오류`);
  }
  return data;
}

export const api = {
  policyStatistics: () => request('GET','/policies/statistics'),
  policies: () => request('GET','/policies'),
  policyVersion: (version,revision) => request('GET',`/policies/${encodeURIComponent(version)}`+(revision?'?revision='+encodeURIComponent(revision):'')),
  policyCompare: (version,base) => request('GET',`/policies/${encodeURIComponent(version)}/compare?base=${encodeURIComponent(base)}`),
  policyImpacts: filters => request('GET','/policies/impacts?'+new URLSearchParams(Object.entries(filters).filter(([,v])=>v!==''))),
  policyHistory: id => request('GET',`/deployments/${id}/policy-history`),
  policyRecheck: (id,target) => request('POST',`/deployments/${id}/policy-rechecks`,{target}),
  policyReview: (id,job_id,policy_digest) => request('POST',`/deployments/${id}/policy-reviews`,{job_id,policy_digest}),
  policyFailureReview: (id,execution,body) => request('POST',`/deployments/${id}/policy-failures/${execution}/review`,body),
  policyRedeploy: (id,commit,policy_digest) => request('POST',`/deployments/${id}/policy-redeploy`,{commit,policy_digest}),
  runtime: () => request("GET", "/runtime"),
  projects: () => request("GET", "/projects"),
  createProject: (body) => request("POST", "/projects", body),
  deployRepo: (body) => request("POST", "/deploy", body),
  deploy: (projectId, demoVersion) => request("POST", `/projects/${projectId}/deploy`,
    demoVersion ? { demo_version: demoVersion } : undefined),
  retry: (deploymentId) => request("POST", `/deployments/${deploymentId}/retry`),
  deployments: (projectId) => request("GET", `/projects/${projectId}/deployments`),
  plans: (deploymentId) => request("GET", `/deployments/${deploymentId}/plans`),
  analysis: (deploymentId) => request("GET", `/deployments/${deploymentId}/analysis`),
  policy: (deploymentId) => request("GET", `/deployments/${deploymentId}/policy`),
  patch: (deploymentId) => request("GET", `/deployments/${deploymentId}/patch`),
  approve: (deploymentId) => request("POST", `/deployments/${deploymentId}/approve`),
  reject: (deploymentId) => request("POST", `/deployments/${deploymentId}/reject`),
  rollback: (deploymentId) => request("POST", `/deployments/${deploymentId}/rollback`),
  verify: (deploymentId) => request("POST", `/deployments/${deploymentId}/verify`),
};

// SSE: 새로고침해도 처음부터 다시 받는다. 서버가 end를 보내면 닫는다.
export function streamEvents(deploymentId, onEvent) {
  const source = new EventSource(`/api/deployments/${deploymentId}/events`);
  source.addEventListener("deploy", (e) => onEvent({ seq: Number(e.lastEventId), ...JSON.parse(e.data) }));
  source.addEventListener("end", () => source.close());
  return () => source.close();
}
