import { useCallback, useEffect, useState } from "react";
import { api, streamEvents } from "./api.js";
import { explain } from "./explain.js";
import { Badge, Pipeline, TARGETS, TERMINAL } from "./Pipeline.jsx";
import { Structure } from "./Structure.jsx";

const TRIGGER = { manual: "수동", push: "git push", rollback: "롤백" };
const MODE = { full_analysis: "전체 분석", reanalyze: "재분석", rebuild_only: "빌드만 (AI 생략)" };

const STATE_KIND = { relational_db: "관계형 DB", persistent_files: "영구 파일" };
const BACKEND = { replay: "저장된 응답 재생", "codex-cli": "로컬 Codex · ChatGPT 로그인", openai: "실제 모델 호출", fixture: "예시 분석 결과", mixed: "혼합" };
const ANALYSIS_STAGE = { mapper: "소스 준비", snapshot: "소스 확인", cached_intent: "이전 분석 확인",
  analyzer: "모델 분석", intent_policy: "분석 결과와 소스 대조", intent_gate: "분석 근거 검사",
  planner: "배포 설계", plan_policy: "배포 설계 검사", context: "분석 저장" };
const POLICY_FIELD = { source_revision: "커밋", app: "앱 이름", unknowns: "미확인 요구사항",
  workloads: "서비스·포트·상태 확인", state: "DB·파일 저장소", secrets: "필수 환경변수", config: "환경설정" };
const ANALYSIS_ERROR = {
  intent_source_mismatch: "모델의 분석 결과가 검토된 소스의 실행 조건과 일치하지 않아요.",
  unreviewed_runtime_source: "현재 실행 정책에서 검토하지 않은 소스가 포함돼 있어요.",
  non_source_evidence: "분석 근거가 실행 소스 대신 다른 파일을 가리켜요.",
  invalid_source_evidence: "분석 근거의 파일·줄을 확인할 수 없어요.",
  plan_source_mismatch: "배포 설계가 검토된 소스의 실행 조건과 일치하지 않아요.",
};

const PLAN_ROWS = [
  ["서비스", (p) => p.services.map((s) => `${s.name} (${s.kind})`).join(", ")],
  ["포트 · 상태 확인", (p) => p.services.filter((s) => s.public).map((s) => `${s.port} · ${s.health ?? "없음"}`).join(", ")],
  ["DB", (p) => p.db?.type ?? "없음"],
  ["파일 저장소", (p) => (p.storage ? `${p.storage.type} · ${p.storage.path}` : "없음")],
  ["로그", (p) => p.logs],
  ["예상 월 비용", (p) => (p.est_monthly_krw == null ? "미정" : `₩${p.est_monthly_krw.toLocaleString()}`)],
];

function load(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}
function save(key, value) {
  try { localStorage.setItem(key, value); } catch { /* 저장 못 해도 화면은 동작 */ }
}

function NewProject({ onCreated, awsEnabled }) {
  const [repo, setRepo] = useState("https://github.com/Team-InfraMorph/demo-app");
  const [branch, setBranch] = useState("main");
  const [targets, setTargets] = useState(awsEnabled ? ["local", "aws"] : ["local"]);
  const [error, setError] = useState("");

  const toggle = (t) => setTargets((cur) => (cur.includes(t) ? cur.filter((x) => x !== t) : [...cur, t]));
  const submit = async (e) => {
    e.preventDefault();
    setError("");
    try {
      onCreated(await api.createProject({ repo_url: repo, branch, targets }));
    } catch (err) {
      setError(err.message);
    }
  };

  return (
    <form className="card form" onSubmit={submit}>
      <h2>새 프로젝트</h2>
      <label>GitHub 레포 <input value={repo} onChange={(e) => setRepo(e.target.value)} /></label>
      <label>브랜치 <input value={branch} onChange={(e) => setBranch(e.target.value)} /></label>
      <fieldset>
        <legend>배포 대상</legend>
        {Object.entries(TARGETS).map(([t, label]) => (
          <label key={t} className="check">
            <input type="checkbox" disabled={t === "aws" && !awsEnabled} checked={targets.includes(t)} onChange={() => toggle(t)} /> {label}
          </label>
        ))}
      </fieldset>
      {error && <p className="error">{error}</p>}
      <button type="submit" disabled={!targets.length}>만들기</button>
    </form>
  );
}

function ApprovalBanner({ deployment, onAct }) {
  return (
    <div className="card approval">
      <h2>인프라가 바뀝니다. 승인할까요?</h2>
      <ul>{deployment.approval_reasons.map((r) => <li key={r}>{r}</li>)}</ul>
      <div className="row">
        <button onClick={() => onAct(api.approve)}>승인하고 배포</button>
        <button className="secondary" onClick={() => onAct(api.reject)}>거절</button>
      </div>
    </div>
  );
}

function Usage({ deployment, metrics }) {
  if (deployment.analysis_mode === "rebuild_only" && !metrics?.recovery?.attempts) {
    return <p className="usage saved">AI 분석 생략: 이전 분석을 재사용 (모델 호출 0회, 비용 $0)</p>;
  }
  if (!metrics) return null;
  const parts = [`${metrics.backend === "replay" ? "응답 재생" : "모델 호출"} ${metrics.model_calls ?? 0}회`];
  if (metrics.backend !== "replay" && metrics.model) parts.unshift(metrics.model);
  if (metrics.api_calls != null) parts.push(`API 호출 ${metrics.api_calls}회`);
  if (metrics.usage_complete === false) parts.push("사용량 집계 미완료");
  if (metrics.tool_calls != null) parts.push(`파일 탐색 ${metrics.tool_calls}회`);
  if (metrics.validation_retries) parts.push(`분석 보정 ${metrics.validation_retries}회`);
  if (metrics.app_name_corrections) parts.push(`소스에서 앱 이름 확정 ${metrics.app_name_corrections}회`);
  if (metrics.source_clarifications) parts.push(`미확인 요구사항 재검토 ${metrics.source_clarifications}회`);
  if (metrics.input_tokens || metrics.output_tokens) parts.push(`토큰 ${metrics.input_tokens}/${metrics.output_tokens}`);
  if (metrics.backend === "codex-cli") parts.push("ChatGPT 사용량 사용 · 팀 API 비용 $0");
  else if (metrics.estimated_usd != null) parts.push(`예상 $${Number(metrics.estimated_usd).toFixed(3)}`);
  if (metrics.duration_ms != null) parts.push(`${(metrics.duration_ms / 1000).toFixed(1)}초`);
  const fields = (Array.isArray(metrics.policy_fields) ? metrics.policy_fields : [])
    .filter((name) => Object.hasOwn(POLICY_FIELD, name)).map((name) => POLICY_FIELD[name]);
  return <>
    {metrics.error && <p className="usage error">
      AI 분석 실패: {ANALYSIS_ERROR[metrics.error] ?? explain(metrics.error)?.what ?? "분석 처리를 완료하지 못했어요."}
      {ANALYSIS_STAGE[metrics.blocked_stage] && <> 단계: {ANALYSIS_STAGE[metrics.blocked_stage]}.</>}
      {fields.length > 0 && <> 확인 항목: {fields.join(", ")}.</>}
      <span className="dim"> ({metrics.error})</span>
    </p>}
    <p className="usage">{parts.join(" · ")} <span className="dim">({BACKEND[metrics.backend] ?? metrics.backend})</span></p>
  </>;
}

function Recovery({ metrics }) {
  const recovery = metrics?.recovery;
  if (!recovery) return null;
  const recovered = recovery.status === "recovered";
  const reason = { second_local_failure: "재시도 후에도 자동 테스트가 실패했어요.",
    reanalysis_failed: "재분석을 완료하지 못했어요.", not_retryable: "자동 재시도 대상이 아닌 실패예요.",
    recovery_timeout: "자동 복구 시간이 초과됐어요." }[recovery.reason];
  return <div className={`card ${recovered ? "" : "error"}`}>
    <h2>{recovered ? "자동 복구 완료" : "자동 복구 중단"}</h2>
    <p>재시도 {recovery.attempts}회 / 최대 1회 · {recovered ? "검증을 통과한 분석 결과와 설계도를 반영했어요." : "기존 분석 결과와 설계도를 유지했어요."}</p>
    {reason && <p>{reason}</p>}
    <p className="dim">초기 응답 {recovery.initial_metrics.model_calls ?? 0}회 · 재분석 응답 {recovery.retry_metrics.model_calls ?? 0}회 · 총 사용량은 아래에 합산돼요.</p>
  </div>;
}

function AnalysisCard({ deployment, intent }) {
  return (
    <>
    <Recovery metrics={deployment.analysis_metrics} />
    <div className="card">
      <h2>AI가 이해한 앱</h2>
      <Usage deployment={deployment} metrics={deployment.analysis_metrics} />
      {intent && (
        <table className="intent">
          <tbody>
            {intent.workloads.map((w) => (
              <tr key={w.name}>
                <th>{w.kind === "http" ? "웹 서비스" : "백그라운드 작업"}</th>
                <td>
                  {w.name}{w.port ? ` · 포트 ${w.port}` : ""}{w.public ? " · 외부 공개" : ""}{w.command ? ` · ${w.command}` : ""}
                  <div className="evidence">근거: {w.evidence.join(", ")}</div>
                </td>
              </tr>
            ))}
            {intent.state.map((st) => (
              <tr key={st.kind + (st.path ?? "")}>
                <th>{STATE_KIND[st.kind] ?? st.kind}</th>
                <td>
                  {[st.engine, st.orm, st.path].filter(Boolean).join(" · ")}
                  {st.reason && <div>{st.reason}</div>}
                  <div className="evidence">근거: {st.evidence.join(", ")}</div>
                </td>
              </tr>
            ))}
            {intent.secrets.length > 0 && (
              <tr><th>비밀값</th><td>{intent.secrets.join(", ")} <span className="dim">(이름만, 값은 배포 때 주입)</span></td></tr>
            )}
          </tbody>
        </table>
      )}
    </div>
    </>
  );
}

const ACTION = { add: "추가", modify: "수정" };

function PatchCard({ patch }) {
  const entries = Object.entries(patch);
  if (!entries.length) return null;
  const same = entries.length > 1 && entries.every(([, p]) => JSON.stringify(p) === JSON.stringify(entries[0][1]));
  const shown = same ? [["공통 변경", entries[0][1]]] : entries;
  return (
    <div className="card">
      <h2>코드를 이렇게 고쳤다</h2>
      {shown.map(([target, value]) => <section key={target}>
      <h3>{TARGETS[target] ?? target} {value.phase === "recovery" ? "· 자동 복구 후 패치" : ""}</h3>
      {value.verified && <p className="dim">E 정책 검사 통과 · {value.applied ? "이 배포에서 실행 검증 완료" : "실행 검증이 완료되지 않은 변경"} · 커밋 {value.source_revision.slice(0, 7)}</p>}
      {value.initial && <p className="dim">최초 패치 이력을 보존하고 복구에 성공한 패치를 표시합니다.</p>}
      {value.status === "unchanged" && <p className="dim">수정할 코드가 없습니다.</p>}
      {value.files.map((f) => (
        <details key={f.path} className="patch-file">
          <summary><span className="mono">{f.path}</span> <span className="dim">{ACTION[f.action] ?? f.action}</span></summary>
          {f.diff == null ? <p className="dim">잠금 파일이라 내용은 생략</p> : <>
            <pre className="diff">{f.diff.split("\n").map((line, i) => (
              <span key={i} className={line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : ""}>{line}{"\n"}</span>
            ))}</pre>
            {f.truncated && <p className="dim">표시 크기 제한으로 diff 일부를 생략했습니다.</p>}
          </>}
        </details>
      ))}
      </section>)}
    </div>
  );
}

function PlanCompare({ plans, targets }) {
  if (!targets.every((t) => plans[t])) return null;
  return (
    <div className="card">
      <h2>같은 앱, 대상별 설계도</h2>
      <table className="compare">
        <thead><tr><th />{targets.map((t) => <th key={t}>{TARGETS[t]}</th>)}</tr></thead>
        <tbody>
          {PLAN_ROWS.map(([label, read]) => (
            <tr key={label}><th>{label}</th>{targets.map((t) => <td key={t}>{read(plans[t])}</td>)}</tr>
          ))}
        </tbody>
      </table>
      <div className="structures">
        {targets.map((t) => (
          <div key={t}>
            <h3>{TARGETS[t]} 구조</h3>
            <Structure plan={plans[t]} />
          </div>
        ))}
      </div>
    </div>
  );
}

function History({ deployments, selectedId, onSelect, onRollback }) {
  return (
    <div className="card">
      <h2>배포 이력</h2>
      <table className="history">
        <thead><tr><th>시각</th><th>시작</th><th>커밋</th><th>처리 깊이</th><th>상태</th><th /></tr></thead>
        <tbody>
          {deployments.map((d) => (
            <tr key={d.id} className={d.id === selectedId ? "selected" : ""} onClick={() => onSelect(d.id)}>
              <td>{new Date(d.created_at).toLocaleTimeString()}</td>
              <td>{TRIGGER[d.triggered_by] ?? d.triggered_by}</td>
              <td className="mono">{d.commit_sha ? d.commit_sha.slice(0, 7) : "—"}</td>
              <td title={d.change_reasons.join("\n")}>{MODE[d.analysis_mode] ?? "—"}</td>
              <td><Badge status={d.status} /></td>
              <td>
                {["LIVE", "FAILED", "ROLLED_BACK"].includes(d.status) && (
                  <button className="small secondary" onClick={(e) => { e.stopPropagation(); onRollback(d.id); }}>
                    이전 버전으로
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function App() {
  const [runtime, setRuntime] = useState(null);
  const [projects, setProjects] = useState([]);
  const [projectId, setProjectId] = useState(load("projectId"));
  const [deployments, setDeployments] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [events, setEvents] = useState([]);
  const [plans, setPlans] = useState({});
  const [intent, setIntent] = useState(null);
  const [patch, setPatch] = useState({});
  const [error, setError] = useState("");

  const project = projects.find((p) => p.project_id === projectId);
  const selected = deployments.find((d) => d.id === selectedId) ?? deployments[0];

  useEffect(() => {
    let alive = true;
    api.runtime().then((value) => alive && setRuntime(value)).catch(() => {});
    return () => { alive = false; };
  }, []);

  const refresh = useCallback(async () => {
    try {
      const list = await api.projects();
      setProjects(list);
      if (projectId && list.some((p) => p.project_id === projectId)) {
        setDeployments(await api.deployments(projectId));
      } else if (projectId || (projectId === null && list.length)) {
        // 처음 방문(null)이거나 저장된 프로젝트가 없어졌으면(DB 초기화) 최신 것으로. "+ 새 프로젝트"를 고르면 ""라 그대로 둔다.
        setProjectId(list[0]?.project_id ?? "");
      }
    } catch (err) {
      setError(`조종실 서버에 연결할 수 없습니다: ${err.message}`);
      return;
    }
    setError("");
  }, [projectId]);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 1500);
    return () => clearInterval(timer);
  }, [refresh]);

  const selectedKey = selected?.id;
  const selectedStatus = selected?.status;
  useEffect(() => {
    if (!selectedKey) return undefined;
    setEvents([]);
    return streamEvents(selectedKey, (event) =>
      setEvents((cur) => (cur.some((e) => e.seq === event.seq) ? cur : [...cur, event])));
  }, [selectedKey]);

  useEffect(() => {
    if (!selectedKey) return;
    let alive = true;
    setPlans({});
    setIntent(null);
    setPatch({});
    api.plans(selectedKey).then((value) => alive && setPlans(value)).catch(() => alive && setPlans({}));
    api.analysis(selectedKey).then((a) => alive && setIntent(a.intent)).catch(() => alive && setIntent(null));
    api.patch(selectedKey).then((value) => alive && setPatch(value)).catch(() => alive && setPatch({}));
    return () => { alive = false; };
  }, [selectedKey, selectedStatus]);

  const choose = (id) => {
    setProjectId(id);
    setSelectedId(null);
    setDeployments([]);
    setPlans({});
    setPatch({});
    setIntent(null);
    setEvents([]);
    save("projectId", id ?? "");
  };
  const act = async (fn, id = selected?.id) => {
    try {
      await fn(id);
      setSelectedId(null); // 조작 후에는 가장 최신 배포를 따라간다
      refresh();
    } catch (err) {
      setError(err.message);
    }
  };

  const targets = project?.targets ?? [];
  const running = selected && !TERMINAL.includes(selected.status) && selected.status !== "CREATED";

  return (
    <div className="app">
      <header>
        <h1>InfraMorph 조종실</h1>
        <select value={projectId ?? ""} onChange={(e) => choose(e.target.value)}>
          <option value="">+ 새 프로젝트</option>
          {projects.map((p) => (
            <option key={p.project_id} value={p.project_id}>{p.repo_url.replace("https://github.com/", "")} · {p.branch} · {p.targets.map((t) => t === "aws" ? "AWS" : "Local").join(" / ")} · {p.project_id.slice(-6)}</option>
          ))}
        </select>
      </header>
      {runtime?.analysis_backend === "codex-cli" && <p className="usage">
        로컬 Codex 실제 분석 · {runtime.model} / {runtime.reasoning_effort} · ChatGPT 사용량 사용 · 팀 API 비용 $0
      </p>}
      {runtime?.aws_enabled && <p className="usage">AWS 실제 배포 연결됨 · 프로젝트별로 앱과 데이터를 분리해 배포합니다.</p>}
      {error && <p className="error banner">{error}</p>}

      {!project && <NewProject awsEnabled={runtime?.aws_enabled} onCreated={(p) => { choose(p.project_id); refresh(); }} />}

      {project && (
        <>
          <div className="row between toolbar">
            <div>
              <strong>{project.repo_url.replace("https://github.com/", "")}</strong>
              <span className="dim"> · {project.branch}</span>
            </div>
            <button disabled={running} onClick={() => act(api.deploy, project.project_id)}>
              {!running ? "배포" : selected.status === "AWAITING_APPROVAL" ? "승인 대기 중" : "배포 중…"}
            </button>
          </div>

          {selected?.status === "AWAITING_APPROVAL" && <ApprovalBanner deployment={selected} onAct={act} />}
          {selected?.status === "FAILED" && intent && <p>
            <button className="secondary" disabled={running} onClick={() => act(api.retry)}>같은 분석으로 다시 배포</button>
            <span className="dim"> 검증된 동일 커밋의 분석을 재사용하고 소스와 배포 조건을 다시 검사합니다.</span>
          </p>}

          {selected && (
            <div className="row between summary">
              <span>
                {TRIGGER[selected.triggered_by]} · {selected.commit_sha?.slice(0, 7) ?? "커밋 미정"}
                {selected.analysis_mode && ` · ${MODE[selected.analysis_mode]}`}
              </span>
              <Badge status={selected.status} />
            </div>
          )}
          {selected?.change_reasons.length > 0 && (
            <p className="dim reasons">판정 근거: {selected.change_reasons.join(" / ")}</p>
          )}

          {selected && (
            <Pipeline deployment={selected} events={events} targets={targets}
                      onRecheck={() => api.verify(selected.id).then(refresh).catch((err) => setError(err.message))} />
          )}

          {selected && <AnalysisCard deployment={selected} intent={intent} />}

          <PlanCompare plans={plans} targets={targets} />

          <PatchCard patch={patch} />


          <History deployments={deployments} selectedId={selected?.id}
                   onSelect={(id) => setSelectedId(id === deployments[0]?.id ? null : id)}
                   onRollback={(id) => act(api.rollback, id)} />
        </>
      )}
    </div>
  );
}
