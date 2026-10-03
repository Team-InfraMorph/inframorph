import { useCallback, useEffect, useState } from "react";
import { api, streamEvents } from "./api.js";
import { explain } from "./explain.js";
import PolicyCard from "./PolicyCard.jsx";
import { Badge, Pipeline, Results, TARGETS, TERMINAL } from "./Pipeline.jsx";
import { AppCode } from "./CodeTree.jsx";
import { PatchCard } from "./Patch.jsx";

const TRIGGER = { manual: "수동", push: "git push", rollback: "롤백" };
const MODE = { full_analysis: "전체 분석", reanalyze: "재분석", rebuild_only: "빌드만 (AI 생략)" };

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

function AnalysisCard({ deployment, intent, repoMap, patch, plans, targets, repo }) {
  return (
    <>
    <Recovery metrics={deployment.analysis_metrics} />
    <div className="card appcard">
      <div className="appcard-head">
        <h2>
          <span className="mono">{repo.replace("https://github.com/", "")}</span>
          {repoMap?.commit && <span className="dim mono"> @{repoMap.commit.slice(0, 7)}</span>}
        </h2>
        <span className="dim">AI가 코드에서 부품을 찾고, 대상마다 맞는 인프라로 바꿔 배포합니다</span>
      </div>
      <AppCode repoMap={repoMap} intent={intent} patch={patch} plans={plans} targets={targets}
               urls={Object.fromEntries(targets.map((t) => [t, deployment.targets[t]?.url]))} />
      <Usage deployment={deployment} metrics={deployment.analysis_metrics} />
    </div>
    </>
  );
}




/** 화면의 각 구역 = 번호 + 제목 + 이 구역을 왜 보여 주는지 한 줄. */
function Section({ n, title, why, children }) {
  return (
    <section className="sec">
      <header className="sec-head">
        <span className="sec-n">{n}</span>
        <div><h2>{title}</h2>{why && <p>{why}</p>}</div>
      </header>
      {children}
    </section>
  );
}

function ago(iso) {
  const sec = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  return sec < 60 ? "방금" : sec < 3600 ? `${Math.floor(sec / 60)}분 전` : sec < 86400 ? `${Math.floor(sec / 3600)}시간 전` : `${Math.floor(sec / 86400)}일 전`;
}

function took(d) {
  if (!TERMINAL.includes(d.status) || !d.updated_at) return null;
  const s = Math.round((Date.parse(d.updated_at) - Date.parse(d.created_at)) / 1000);
  return s < 60 ? `${s}초` : `${Math.floor(s / 60)}분 ${s % 60}초`;
}

/** 데모 모드에서 고정 커밋이면 "V1 "·"V2 " 접두어. */
const versionOf = (versions, sha) => { const v = versions.find((x) => x.commit_sha === sha); return v ? `${v.id.toUpperCase()} ` : ""; };

const TRIGGER_LONG = { manual: "수동 배포", push: "git push", rollback: "되돌리기" };

/** 배포 기록: 최신이 위인 타임라인. 점 색 = 결과, 대상별 결과, 걸린 시간, 다시 한 범위. */
function History({ deployments, selectedId, onSelect, onRollback, versions = [] }) {
  const canRollback = deployments.slice(1).some((x) => x.status === "LIVE");
  return (
    <div className="card hist">
      {!deployments.length && <p className="dim">아직 배포 기록이 없습니다</p>}
      <ol className="hlist">
        {deployments.map((d, i) => (
          <li key={d.id} className={`hitem hs-${d.status}${d.id === selectedId ? " sel" : ""}`} onClick={() => onSelect(d.id)}>
            <span className="hdot" />
            <div className="hmain">
              <div className="hline1">
                <strong>{TRIGGER_LONG[d.triggered_by] ?? d.triggered_by}</strong>
                <code className="hcommit">{versionOf(versions, d.commit_sha)}{d.commit_sha ? d.commit_sha.slice(0, 7) : "커밋 미정"}</code>
                <Badge status={d.status} />
                {i === 0 && <span className="hlatest">최신</span>}
              </div>
              <div className="hline2">
                <span title={new Date(d.created_at).toLocaleString()}>{ago(d.created_at)}</span>
                {took(d) && <span>걸린 시간 {took(d)}</span>}
                <span>{MODE[d.analysis_mode] ?? (d.triggered_by === "rollback" ? "이전 버전 재배포" : "전체 (처음부터)")}</span>
                {Object.entries(d.targets ?? {}).map(([t, v]) => (
                  <span key={t} className={`htgt ht-${v.status}`}>{TARGETS[t]?.split(" ")[0] ?? t} {v.status === "LIVE" ? "✓" : v.status === "FAILED" ? "✗" : "…"}</span>
                ))}
              </div>
              {d.change_reasons?.length > 0 && <div className="hwhy">{d.change_reasons.join(" · ")}</div>}
            </div>
            {i === 0 && canRollback && TERMINAL.includes(d.status) && (
              <button className="small secondary" onClick={(e) => { e.stopPropagation(); onRollback(d.id); }}>직전 정상 버전으로 되돌리기</button>
            )}
          </li>
        ))}
      </ol>
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
  const [repoMap, setRepoMap] = useState(null);
  const [patch, setPatch] = useState({});
  const [policy, setPolicy] = useState(null);
  const [policyError, setPolicyError] = useState(false);
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
    api.analysis(selectedKey).then((a) => { if (alive) { setIntent(a.intent); setRepoMap(a.repo_map ?? null); } })
      .catch(() => alive && setIntent(null));
    api.patch(selectedKey).then((value) => alive && setPatch(value)).catch(() => alive && setPatch({}));
    return () => { alive = false; };
  }, [selectedKey, selectedStatus]);

  useEffect(() => {
    setPolicy(null); setPolicyError(false);
    if (!selectedKey) return;
    let alive = true;
    const loadPolicy = () => api.policy(selectedKey).then(value => {
      if (alive) { setPolicy(value); setPolicyError(false); }
    }).catch(() => { if (alive) setPolicyError(true); });
    loadPolicy();
    const timer = setInterval(loadPolicy, 1500);
    return () => { alive = false; clearInterval(timer); };
  }, [selectedKey]);

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
          <div className="toolbar">
            <div className="toolbar-main">
              <strong className="repo">{project.repo_url.replace("https://github.com/", "")}</strong>
              <span className="dim"> · {project.branch}</span>
              {selected && (
                <span className="meta">
                  {TRIGGER[selected.triggered_by]} · <span className="mono">{versionOf(versions, selected.commit_sha)}{selected.commit_sha?.slice(0, 7) ?? "커밋 미정"}</span>
                  {selected.analysis_mode && ` · ${MODE[selected.analysis_mode]}`}
                </span>
              )}
              {selected && <Badge status={selected.status} />}
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
          {selected?.change_reasons.length > 0 && (
            <p className="dim reasons">판정 근거: {selected.change_reasons.join(" / ")}</p>
          )}
          {canSelectVersion && <p className="dim">선택한 버전의 고정된 데모 소스로 배포합니다.
            {deployVersion === "v2" && " V2는 기존 웹 앱에 노트 수를 집계하는 worker가 추가됩니다."}</p>}

          {selected?.status === "AWAITING_APPROVAL" && <ApprovalBanner deployment={selected} onAct={act} />}
          {selected?.status === "FAILED" && intent && <p>
            <button className="secondary" disabled={running} onClick={() => act(api.retry)}>같은 분석으로 다시 배포</button>
            <span className="dim"> 검증된 동일 커밋의 분석을 재사용하고 소스와 배포 조건을 다시 검사합니다.</span>
          </p>}

          {selected && (
            <Section n="1" title="배포 과정" why="칸을 누르면 그 단계의 기록이 열립니다">
              <Pipeline deployment={selected} events={events} targets={targets}
                        ctx={{ intent, patch, plans, repo: project.repo_url, commit: selected.commit_sha ?? repoMap?.commit }} />
            </Section>
          )}

          {selected && (
            <Section n="2" title="결과 · 접속 주소">
              <Results deployment={selected} events={events} targets={targets}
                       onRecheck={() => api.verify(selected.id).then(refresh).catch((err) => setError(err.message))} />
            </Section>
          )}

          {selected && (
            <Section n="3" title="배포할 앱과 배포된 구조" why="파일이나 부품에 마우스를 올리면 서로 연결된 곳이 표시됩니다">
              <AnalysisCard deployment={selected} intent={intent} repoMap={repoMap} patch={patch} plans={plans} targets={targets} repo={project.repo_url} />
            </Section>
          )}

          {(Object.keys(patch).length > 0 || policy?.results?.length > 0 || policyError) && (
            <Section n="4" title="코드 변경 · 정책 검사">
              <PolicyCard data={policy} error={policyError} />
              {Object.keys(patch).length > 0 && <PatchCard patch={patch} />}
            </Section>
          )}

          <Section n="5" title="배포 기록 · 되돌리기">
            <History deployments={deployments} selectedId={selected?.id} versions={versions}
                     onSelect={(id) => setSelectedId(id === deployments[0]?.id ? null : id)}
                     onRollback={(id) => act(api.rollback, id)} />
          </Section>
        </>
      )}
    </div>
  );
}
