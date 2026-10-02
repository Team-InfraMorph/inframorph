import { useCallback, useEffect, useState } from "react";
import { api, streamEvents } from "./api.js";
import { Structure } from "./Structure.jsx";

const TARGETS = { local: "Local (노트북 Docker)", aws: "AWS (서울)" };
const STEPS = {
  snapshot: "스냅샷", map: "레포 지도", analyze: "AI 분석", policy: "정책 검사", plan: "설계",
  patch: "코드 수정", build: "빌드", push: "이미지 업로드", infra: "인프라", start: "실행",
  health: "상태 확인", url: "주소 발급", smoke: "자동 테스트", rollback: "롤백",
};
const STATUS = {
  CREATED: "대기", DEPLOYING: "배포 중", AWAITING_APPROVAL: "승인 대기", LIVE: "정상",
  FAILED: "실패", ROLLED_BACK: "롤백됨", SUPERSEDED: "건너뜀",
};
const TRIGGER = { manual: "수동", push: "git push", rollback: "롤백" };
const MODE = { full_analysis: "전체 분석", reanalyze: "재분석", rebuild_only: "빌드만 (AI 생략)" };
const TERMINAL = ["LIVE", "FAILED", "ROLLED_BACK", "SUPERSEDED"];

const STATE_KIND = { relational_db: "관계형 DB", persistent_files: "영구 파일" };
const BACKEND = { replay: "저장된 응답 재생", openai: "실제 모델 호출", fixture: "예시 분석 결과" };

const PLAN_ROWS = [
  ["서비스", (p) => p.services.map((s) => `${s.name} (${s.kind})`).join(", ")],
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

function Badge({ status }) {
  return <span className={`badge s-${status}`}>{STATUS[status] ?? status}</span>;
}

function NewProject({ onCreated }) {
  const [repo, setRepo] = useState("https://github.com/Team-InfraMorph/demo-app");
  const [branch, setBranch] = useState("main");
  const [targets, setTargets] = useState(["local", "aws"]);
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
            <input type="checkbox" checked={targets.includes(t)} onChange={() => toggle(t)} /> {label}
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
  if (deployment.analysis_mode === "rebuild_only") {
    return <p className="usage saved">AI 분석 생략: 이전 분석을 재사용 (모델 호출 0회, 비용 $0)</p>;
  }
  if (!metrics) return null;
  if (metrics.error) return <p className="usage error">AI 분석 실패: {metrics.error}</p>;
  const parts = [`모델 호출 ${metrics.model_calls ?? 0}회`];
  if (metrics.tool_calls != null) parts.push(`파일 탐색 ${metrics.tool_calls}회`);
  if (metrics.input_tokens || metrics.output_tokens) parts.push(`토큰 ${metrics.input_tokens}/${metrics.output_tokens}`);
  if (metrics.estimated_usd != null) parts.push(`예상 $${Number(metrics.estimated_usd).toFixed(3)}`);
  if (metrics.duration_ms != null) parts.push(`${(metrics.duration_ms / 1000).toFixed(1)}초`);
  return <p className="usage">{parts.join(" · ")} <span className="dim">({BACKEND[metrics.backend] ?? metrics.backend})</span></p>;
}

function AnalysisCard({ deployment, intent }) {
  return (
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
  );
}

const ACTION = { add: "추가", modify: "수정" };

function PatchCard({ patch }) {
  const [first] = Object.values(patch);
  if (!first) return null;
  const same = Object.values(patch).every((p) => JSON.stringify(p) === JSON.stringify(first));
  return (
    <div className="card">
      <h2>코드를 이렇게 고쳤다 {same && <span className="dim">· Local·AWS 동일, 실행 때 환경변수로 저장소 선택</span>}</h2>
      {first.files.map((f) => (
        <details key={f.path} className="patch-file">
          <summary><span className="mono">{f.path}</span> <span className="dim">{ACTION[f.action] ?? f.action}</span></summary>
          {f.diff == null ? <p className="dim">잠금 파일이라 내용은 생략</p> : (
            <pre className="diff">{f.diff.split("\n").map((line, i) => (
              <span key={i} className={line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : ""}>{line}{"\n"}</span>
            ))}</pre>
          )}
        </details>
      ))}
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

function TargetColumn({ target, state, events }) {
  return (
    <div className="card target">
      <div className="row between">
        <h2>{TARGETS[target]}</h2>
        {state && <Badge status={state.status} />}
      </div>
      {state?.url && <a className="url" href={state.url} target="_blank" rel="noreferrer">{state.url}</a>}
      <ol className="timeline">
        {events.map((e) => (
          <li key={e.seq} className={`ev-${e.status}`}>
            <span className="step">{STEPS[e.step] ?? e.step}</span>
            <span className="mark">{e.status === "ok" ? "완료" : e.status === "fail" ? "실패" : "시작"}</span>
            {e.duration_ms != null && <span className="dim">{(e.duration_ms / 1000).toFixed(1)}초</span>}
            {e.detail && <div className="detail">{e.detail}</div>}
          </li>
        ))}
        {!events.length && <li className="dim">아직 이벤트가 없습니다</li>}
      </ol>
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
    api.plans(selectedKey).then(setPlans).catch(() => setPlans({}));
    api.analysis(selectedKey).then((a) => setIntent(a.intent)).catch(() => setIntent(null));
    api.patch(selectedKey).then(setPatch).catch(() => setPatch({}));
  }, [selectedKey, selectedStatus]);

  const choose = (id) => {
    setProjectId(id);
    setSelectedId(null);
    setDeployments([]);
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
        <select value={projectId ?? ""} onChange={(e) => choose(e.target.value || null)}>
          <option value="">+ 새 프로젝트</option>
          {projects.map((p) => (
            <option key={p.project_id} value={p.project_id}>{p.repo_url.replace("https://github.com/", "")} · {p.branch}</option>
          ))}
        </select>
      </header>
      {error && <p className="error banner">{error}</p>}

      {!project && <NewProject onCreated={(p) => { choose(p.project_id); refresh(); }} />}

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

          {selected && <AnalysisCard deployment={selected} intent={intent} />}

          <PlanCompare plans={plans} targets={targets} />

          <PatchCard patch={patch} />

          <div className="targets">
            {targets.map((t) => (
              <TargetColumn key={t} target={t} state={selected?.targets[t]} events={events.filter((e) => e.target === t)} />
            ))}
          </div>

          <History deployments={deployments} selectedId={selected?.id}
                   onSelect={(id) => setSelectedId(id === deployments[0]?.id ? null : id)}
                   onRollback={(id) => act(api.rollback, id)} />
        </>
      )}
    </div>
  );
}
