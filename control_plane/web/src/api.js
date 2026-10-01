async function request(method, path, body) {
  const res = await fetch(`/api${path}`, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join(", ") : data.detail;
    throw new Error(detail || `${res.status} 오류`);
  }
  return data;
}

export const api = {
  projects: () => request("GET", "/projects"),
  createProject: (body) => request("POST", "/projects", body),
  deploy: (projectId) => request("POST", `/projects/${projectId}/deploy`),
  deployments: (projectId) => request("GET", `/projects/${projectId}/deployments`),
  plans: (deploymentId) => request("GET", `/deployments/${deploymentId}/plans`),
  approve: (deploymentId) => request("POST", `/deployments/${deploymentId}/approve`),
  reject: (deploymentId) => request("POST", `/deployments/${deploymentId}/reject`),
  rollback: (deploymentId) => request("POST", `/deployments/${deploymentId}/rollback`),
};

// SSE: 새로고침해도 처음부터 다시 받는다. 서버가 end를 보내면 닫는다.
export function streamEvents(deploymentId, onEvent) {
  const source = new EventSource(`/api/deployments/${deploymentId}/events`);
  source.addEventListener("deploy", (e) => onEvent({ seq: Number(e.lastEventId), ...JSON.parse(e.data) }));
  source.addEventListener("end", () => source.close());
  return () => source.close();
}
