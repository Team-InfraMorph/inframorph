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

function Badge({ status }) {
  return <span className={`badge s-${status}`}>{STATUS[status] ?? status}</span>;
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
  if (metrics.input_tokens || metrics.output_tokens) parts.push(`토큰 ${metrics.input_tokens}/${metrics.output_tokens}`);
  if (metrics.backend === "codex-cli") parts.push("ChatGPT 사용량 사용 · 팀 API 비용 $0");
  else if (metrics.estimated_usd != null) parts.push(`예상 $${Number(metrics.estimated_usd).toFixed(3)}`);
  if (metrics.duration_ms != null) parts.push(`${(metrics.duration_ms / 1000).toFixed(1)}초`);
  const fields = (Array.isArray(metrics.policy_fields) ? metrics.policy_fields : [])
    .filter((name) => Object.hasOwn(POLICY_FIELD, name)).map((name) => POLICY_FIELD[name]);
  return <>
    {metrics.error && <p className="usage error">
      AI 분석 실패: {ANALYSIS_ERROR[metrics.error] ?? "분석 처리를 완료하지 못했어요."}
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

function eventDetail(detail) {
  const messages = {
    image_build_waiting: "다른 배포에서 같은 이미지를 준비하고 있어 완료를 기다립니다.",
    image_build_wait_timeout: "다른 배포의 이미지 빌드가 오래 걸려 대기 시간을 초과했습니다. 완료 후 다시 배포해 주세요.",
    image_build_already_running: "다른 배포가 같은 이미지를 빌드하는 중입니다. 완료 후 다시 배포해 주세요.",
    image_tag_collision: "같은 커밋의 이미지와 수정된 코드가 달라 이미지 검증에 실패했습니다.",
    image_platform_mismatch: "이미지가 배포에 필요한 플랫폼과 일치하지 않습니다.",
    image_provenance_mismatch: "이미지가 승인된 코드로 만들어졌는지 확인하지 못했습니다.",
    untrusted_build_lock: "이미지 빌드 잠금 파일의 안전성을 확인하지 못했습니다.",
    command_failed: "Docker 명령이 실패했습니다. Docker 실행 상태를 확인해 주세요.",
    command_unavailable_or_timeout: "Docker 명령을 실행하지 못했거나 실행 시간을 초과했습니다.",
    local_pipeline_failed: "Local 배포 단계를 완료하지 못했습니다.",
    aws_intent_source_approved: "앱 소스와 분석 결과를 확인했습니다.",
    aws_patch_started: "AWS에 맞게 DB와 저장소 코드를 수정합니다.",
    aws_patch_approved: "AWS 코드 수정의 정책 검사를 통과했습니다.",
    aws_adapter_failed: "AWS 배포 단계를 완료하지 못했습니다. 비공개 진단 기록을 확인해 주세요.",
    "validating exact local linux/amd64 image": "승인된 이미지를 확인합니다.",
    "publishing immutable ECR tag": "승인된 이미지를 ECR에 업로드합니다.",
    "activating digest-pinned ECS services": "ECS 서비스를 실행합니다.",
    "waiting for ALB target health": "로드밸런서에서 앱 상태를 확인합니다.",
    "all registered public targets are healthy": "로드밸런서 상태 검사를 통과했습니다.",
    "performing verified external HTTPS health request": "외부 HTTPS 접속을 확인합니다.",
  };
  if (messages[detail]) return messages[detail];
  try {
    const value = JSON.parse(detail);
    if (messages[value.code]) return messages[value.code];
    if (value.phase === "recoverable_failure") return "자동 테스트 실패를 확인해 한 번 재분석합니다.";
    if (value.code === "retry_recovered") return "재시도 검증을 통과했습니다.";
    if (value.code === "second_local_failure") return "재시도 후에도 실패해 자동 복구를 중단했습니다.";
    if (value.retry_attempt != null) return `자동 복구 ${value.retry_attempt}/1회`;
    if (typeof value.code === "string" && value.code.startsWith("initial_")) return "첫 배포 검증";
    const changes = value.changes ?? value.stage_plan;
    if (changes) return `앱 리소스 추가 ${changes.create} · 수정 ${changes.update} · 삭제 ${changes.delete} · 교체 ${changes.replace}`;
    if (value.mode) return value.mode === "redeploy" ? "기존 앱과 같은 주소로 재배포합니다." : "이 프로젝트 전용 앱을 준비합니다.";
    if (value.services) return `서비스 실행 완료: ${Object.keys(value.services).join(", ")}`;
    if (value.digest) return "이미지를 업로드하고 배포에 사용할 버전을 고정했습니다.";
    if (value.local_image_id) return "승인된 이미지와 커밋을 확인했습니다.";
  } catch { /* E가 보내는 일반 문구도 표시 */ }
  return detail;
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

function Verification({ check, onRecheck }) {
  if (!check) return null;
  const time = new Date(check.checked_at).toLocaleTimeString();
  const text = check.status === "ok" ? `조종실이 직접 확인 · 응답 ${check.code} · ${(check.ms / 1000).toFixed(2)}초`
    : check.status === "fail" ? `직접 확인 실패 · ${check.detail}` : check.detail;
  return (
    <div className={`verify v-${check.status}`}>
      <span>{check.status === "ok" ? "✓" : check.status === "fail" ? "✗" : "–"} {text}</span>
      <span className="dim"> · {time}</span>
      {check.status !== "skipped" && <button className="small secondary" onClick={onRecheck}>다시 확인</button>}
    </div>
  );
}

function TargetColumn({ target, state, events, onRecheck }) {
  return (
    <div className="card target">
      <div className="row between">
        <h2>{TARGETS[target]}</h2>
        {state && <Badge status={state.status} />}
      </div>
      {state?.url && <a className="url" href={state.url} target="_blank" rel="noreferrer">{state.url}</a>}
      <Verification check={state?.verification} onRecheck={onRecheck} />
      <ol className="timeline">
        {events.map((e) => (
          <li key={e.seq} className={`ev-${e.status}`}>
            <span className="step">{STEPS[e.step] ?? e.step}</span>
            <span className="mark">{e.status === "ok" ? "완료" : e.status === "fail" ? "실패" : "시작"}</span>
            {e.duration_ms != null && <span className="dim">{(e.duration_ms / 1000).toFixed(1)}초</span>}
            {e.detail && <div className="detail">{eventDetail(e.detail)}</div>}
          </li>
        ))}
        {!events.length && <li className="dim">아직 이벤트가 없습니다</li>}
      </ol>
    </div>
  );
}

function History({ deployments, selectedId, onSelect, onRollback, versions }) {
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
              <td className="mono">{versions.find((v) => v.commit_sha === d.commit_sha)?.id.toUpperCase()} {d.commit_sha ? d.commit_sha.slice(0, 7) : "—"}</td>
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
  const [requestedVersion, setRequestedVersion] = useState("");
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
    setRequestedVersion("");
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
  const versions = runtime?.demo_versions ?? [];
  const canSelectVersion = versions.length > 0 && project?.repo_url.replace(/\/$/, "").replace(/\.git$/, "") === "https://github.com/Team-InfraMorph/demo-app";
  const deployVersion = requestedVersion || versions.find((v) => v.commit_sha === deployments[0]?.commit_sha)?.id || versions[0]?.id;
  const running = deployments.find((d) => !TERMINAL.includes(d.status) && d.status !== "CREATED");

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
            <div className="row">
              {canSelectVersion && <label className="version-picker">테스트 버전 <select aria-label="테스트 버전" value={deployVersion}
                disabled={running} onChange={(e) => setRequestedVersion(e.target.value)}>
                {versions.map((v) => <option key={v.id} value={v.id}>{v.label}</option>)}
              </select></label>}
              <button disabled={running} onClick={() => act((id) => api.deploy(id, canSelectVersion ? deployVersion : undefined), project.project_id)}>
                {!running ? "배포" : running.status === "AWAITING_APPROVAL" ? "승인 대기 중" : "배포 중…"}
              </button>
            </div>
          </div>
          {canSelectVersion && <p className="dim">선택한 버전의 고정된 데모 소스로 배포합니다.
            {deployVersion === "v2" && " V2는 기존 웹 앱에 노트 수를 집계하는 worker가 추가됩니다."}</p>}

          {selected?.status === "AWAITING_APPROVAL" && <ApprovalBanner deployment={selected} onAct={act} />}
          {selected?.status === "FAILED" && intent && <p>
            <button className="secondary" disabled={running} onClick={() => act(api.retry)}>같은 분석으로 다시 배포</button>
            <span className="dim"> 검증된 동일 커밋의 분석을 재사용하고 소스와 배포 조건을 다시 검사합니다.</span>
          </p>}

          {selected && (
            <div className="row between summary">
              <span>
                {TRIGGER[selected.triggered_by]} · {versions.find((v) => v.commit_sha === selected.commit_sha)?.id.toUpperCase()} {selected.commit_sha?.slice(0, 7) ?? "커밋 미정"}
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
              <TargetColumn key={t} target={t} state={selected?.targets[t]} events={events.filter((e) => e.target === t)}
                            onRecheck={() => api.verify(selected.id).then(refresh).catch((err) => setError(err.message))} />
            ))}
          </div>

          <History deployments={deployments} selectedId={selected?.id} versions={versions}
                   onSelect={(id) => setSelectedId(id === deployments[0]?.id ? null : id)}
                   onRollback={(id) => act(api.rollback, id)} />
        </>
      )}
    </div>
  );
}
