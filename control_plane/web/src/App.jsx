import { useCallback, useEffect, useState } from "react";
import { api, streamEvents } from "./api.js";
import { explain } from "./explain.js";
import PolicyCard from "./PolicyCard.jsx";
import PolicyWorkspace from "./PolicyWorkspace.jsx";
import { Badge, Pipeline, Results, TARGETS, TERMINAL, ordered } from "./Pipeline.jsx";
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

const DEMO_REPO = "https://github.com/Team-InfraMorph/demo-app";
const sameRepo = (a, b) => a.replace(/\/$/, "").replace(/\.git$/, "").toLowerCase() === b.replace(/\/$/, "").replace(/\.git$/, "").toLowerCase();
// 배포 위치: Local 테스트는 항상 먼저 거치고, 통과하면 고른 곳에 배포한다.
const DESTS = { onprem: ["온프레미스", ["local", "onprem"]], aws: ["AWS", ["local", "aws"]],
  both: ["온프레미스 + AWS", ["local", "onprem", "aws"]], none: ["Local 테스트만", ["local"]] };
const destOf = (targets = []) => targets.includes("onprem") && targets.includes("aws") ? "both"
  : targets.includes("aws") ? "aws" : targets.includes("onprem") ? "onprem" : "none";

/** 한 화면에서 레포·브랜치·배포 위치를 고르고 바로 배포한다. 같은 레포·브랜치면 같은 앱으로 이어진다. */
function DeployBar({ runtime, project, running, versions, lastCommit, onDeploy }) {
  const [repo, setRepo] = useState(project?.repo_url ?? DEMO_REPO);
  const [branch, setBranch] = useState(project?.branch ?? "main");
  const [dest, setDest] = useState(null);
  const [version, setVersion] = useState("");
  useEffect(() => {
    if (project) { setRepo(project.repo_url); setBranch(project.branch); setDest(null); setVersion(""); }
  }, [project?.project_id]);
  const allowed = { onprem: runtime?.onprem_enabled, aws: runtime?.aws_enabled,
    both: runtime?.onprem_enabled && runtime?.aws_enabled, none: true };
  const remembered = project && sameRepo(project.repo_url, repo) && project.branch === branch ? destOf(project.targets) : null;
  const current = dest && allowed[dest] ? dest
    : [remembered, "aws", "onprem", "none"].find((d) => d && allowed[d]);
  const demo = versions.length > 0 && sameRepo(repo, DEMO_REPO);
  const picked = version || versions.find((v) => v.commit_sha === lastCommit)?.id || versions[0]?.id;
  const submit = (e) => {
    e.preventDefault();
    onDeploy({ repo_url: repo.trim(), branch: branch.trim(), targets: DESTS[current][1],
               ...(demo ? { demo_version: picked } : {}) });
  };
  return (
    <form className="card deploybar" onSubmit={submit}>
      <label className="db-repo">GitHub 레포 <input value={repo} onChange={(e) => setRepo(e.target.value)} /></label>
      <label className="db-branch">브랜치 <input value={branch} onChange={(e) => setBranch(e.target.value)} /></label>
      <div className="db-dest">
        <span className="db-label">배포 위치</span>
        <div className="seg" role="radiogroup" aria-label="배포 위치">
          <span className="seg-fixed" title="항상 먼저 거칩니다">Local 테스트 →</span>
          {Object.entries(DESTS).filter(([k]) => k !== "none").map(([k, [label]]) => (
            <button type="button" key={k} role="radio" aria-checked={current === k} disabled={!allowed[k]}
                    className={`seg-btn sb-${k}${current === k ? " on" : ""}`} onClick={() => setDest(k)}
                    title={allowed[k] ? undefined : "이 조종실에 연결 설정이 없습니다"}>{label}</button>
          ))}
          <button type="button" role="radio" aria-checked={current === "none"} className={`seg-btn${current === "none" ? " on" : ""}`}
                  onClick={() => setDest("none")}>테스트만</button>
        </div>
      </div>
      {demo && <label className="db-version" title={runtime?.mapper_mode === "github" ? "선택한 버전의 커밋을 GitHub에서 가져와 배포합니다" : "선택한 버전의 고정된 데모 소스로 배포합니다"}>
        테스트 버전{runtime?.mapper_mode === "github" ? " · GitHub" : ""} <select value={picked} onChange={(e) => setVersion(e.target.value)}>
        {versions.map((v) => <option key={v.id} value={v.id}>{v.label}</option>)}
      </select></label>}
      <button type="submit" className="db-go" disabled={Boolean(running)}>
        {!running ? "배포" : running.status === "AWAITING_APPROVAL" ? "승인 대기 중" : "배포 중…"}
      </button>
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
      {ANALYSIS_STAGE[metrics.blocked_stage] ?? "AI 분석"} 실패: {ANALYSIS_ERROR[metrics.error] ?? explain(metrics.error)?.what ?? "해당 단계를 완료하지 못했어요."}
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

const SHORT_T = { local: "Local", onprem: "온프레미스", aws: "AWS" };

/** 배포 기록(왼쪽): 배포 한 번 = 한 줄. 누르면 그 배포의 과정·결과·구조가 그대로 다시 열린다. */
function History({ deployments, selectedId, following, onSelect, onFollow, onRollback, versions = [] }) {
  const canRollback = deployments.slice(1).some((x) => x.status === "LIVE");
  return (
    <aside className="card hist side">
      <div className="hist-head">
        <h2>배포 기록 <span className="dim">{deployments.length}건</span></h2>
        <button className={`small ${following ? "secondary" : ""}`} disabled={following} onClick={onFollow}
                title="새 배포가 시작되면 자동으로 따라갑니다">{following ? "최신 따라가는 중" : "최신으로"}</button>
      </div>
      {!deployments.length && <p className="dim">아직 배포 기록이 없습니다</p>}
      <ol className="hlist">
        {deployments.map((d, i) => (
          <li key={d.id} className={`hitem hs-${d.status}${d.id === selectedId ? " sel" : ""}`} onClick={() => onSelect(d.id)}>
            <span className="hdot" />
            <div className="hmain">
              <div className="hline1">
                <code className="hid" title={d.id}>{d.id}</code>
                <Badge status={d.status} />
                {i === 0 && <span className="hlatest">최신</span>}
              </div>
              <div className="hline2">
                <span title={new Date(d.created_at).toLocaleString()}>{new Date(d.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} · {ago(d.created_at)}</span>
                {took(d) && <span>{took(d)}</span>}
                <span>{TRIGGER_LONG[d.triggered_by] ?? d.triggered_by}</span>
                <span className="mono">{versionOf(versions, d.commit_sha)}{d.commit_sha ? d.commit_sha.slice(0, 7) : "커밋 미정"}</span>
              </div>
              <div className="htgts">
                {ordered(Object.keys(d.targets ?? {})).map((t) => (
                  <span key={t} className={`htgt hg-${t} ht-${d.targets[t].status}`}>{SHORT_T[t] ?? t} {d.targets[t].status === "LIVE" ? "✓" : d.targets[t].status === "FAILED" ? "✗" : "…"}</span>
                ))}
              </div>
              {i === 0 && canRollback && TERMINAL.includes(d.status) && (
                <button className="small secondary hroll" onClick={(e) => { e.stopPropagation(); onRollback(d.id); }}>직전 정상 버전으로 되돌리기</button>
              )}
            </div>
          </li>
        ))}
      </ol>
    </aside>
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
  const [repoMap, setRepoMap] = useState(null);
  const [patch, setPatch] = useState({});
  const [patchLoading, setPatchLoading] = useState(false);
  const [patchError, setPatchError] = useState(false);
  const [policy, setPolicy] = useState(null);
  const [policyError, setPolicyError] = useState(false);
  const [policyHistory,setPolicyHistory]=useState(null);
  const [route,setRoute]=useState(location.hash.slice(1)||'/deploy');
  useEffect(()=>{
    let scroll=0,previous=location.hash.slice(1)||'/deploy';
    const change=()=>{const next=location.hash.slice(1)||'/deploy';if(next.startsWith('/policy')&&!previous.startsWith('/policy'))scroll=window.scrollY;setRoute(next);if(!next.startsWith('/policy')&&previous.startsWith('/policy'))requestAnimationFrame(()=>window.scrollTo(0,scroll));else if(!next.includes('section='))window.scrollTo(0,0);previous=next;};
    window.addEventListener('hashchange',change);return()=>window.removeEventListener('hashchange',change);
  },[]);
  const [error, setError] = useState("");
  const [linkedError,setLinkedError]=useState(''),[linkedLoading,setLinkedLoading]=useState(false);
  useEffect(()=>{
    const id=route.startsWith('/deploy?')?new URLSearchParams(route.split('?')[1]).get('deployment_id'):null;
    if(!id){setLinkedError('');setLinkedLoading(false);return;}
    let alive=true;setSelectedId(id);setDeployments([]);setLinkedError('');setLinkedLoading(true);
    api.deployment(id).then(async deployment=>{
      const list=await api.deployments(deployment.project_id);
      if(!list.some(row=>row.id===id))throw new Error('연결된 배포 기록이 목록에 없습니다.');
      if(alive){setProjectId(deployment.project_id);setDeployments(list);setSelectedId(id);save('projectId',deployment.project_id);}
    }).catch(()=>{if(alive)setLinkedError('연결된 배포를 불러오지 못했습니다. 다른 배포를 해결 결과로 대신 표시하지 않습니다.');})
      .finally(()=>{if(alive)setLinkedLoading(false);});
    return()=>{alive=false;};
  },[route]);

  const project = projects.find((p) => p.project_id === projectId);
  const selected = linkedLoading||linkedError?null:selectedId?deployments.find((d) => d.id === selectedId):deployments[0];

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
    setRepoMap(null);
    setPatch({});
    api.plans(selectedKey).then((value) => alive && setPlans(value)).catch(() => alive && setPlans({}));
    api.analysis(selectedKey).then((a) => { if (alive) { setIntent(a.intent); setRepoMap(a.repo_map ?? null); } })
      .catch(() => { if (alive) { setIntent(null); setRepoMap(null); } });
    setPatchLoading(true);
    setPatchError(false);
    api.patch(selectedKey).then((value) => { if (alive) setPatch(value); })
      .catch(() => { if (alive) setPatchError(true); })
      .finally(() => { if (alive) setPatchLoading(false); });
    return () => { alive = false; };
  }, [selectedKey, selectedStatus]);

  useEffect(() => {
    setPolicy(null); setPolicyHistory(null); setPolicyError(false);
    if (!selectedKey) return;
    let alive = true;
    const loadPolicy = () => Promise.all([api.policy(selectedKey),api.policyHistory(selectedKey)]).then(([value,history]) => {
      if (alive) { setPolicy(value); setPolicyHistory(history); setPolicyError(false); }
    }).catch(() => { if (alive) setPolicyError(true); });
    loadPolicy();
    const timer = setInterval(loadPolicy, 1500);
    return () => { alive = false; clearInterval(timer); };
  }, [selectedKey]);

  const choose = (id) => {
    setProjectId(id);
    setSelectedId(null);
    setDeployments([]);
    setPlans({});
    setPatch({});
    setIntent(null);
    setRepoMap(null);
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

  // 지난 배포는 그때 실제로 있던 대상만 보여 준다(나중에 대상을 추가해도 옛 기록에 빈 칸이 생기지 않게).
  const targets = ordered(Object.keys(selected?.targets ?? {}).length ? Object.keys(selected.targets) : project?.targets ?? []);
  const versions = runtime?.demo_versions ?? [];
  const running = deployments.find((d) => !TERMINAL.includes(d.status) && d.status !== "CREATED");

  return (
    <div className="app">
      <header>
        <h1>InfraMorph 조종실</h1>
        {projects.length > 0 && <select aria-label="배포 기록을 볼 레포" value={projectId ?? ""} onChange={(e) => choose(e.target.value)}>
          {projects.map((p) => (
            <option key={p.project_id} value={p.project_id}>{p.repo_url.replace("https://github.com/", "")} · {p.branch} · {ordered(p.targets).map((t) => ({ local: "Local 테스트", onprem: "온프레미스", aws: "AWS" })[t] ?? t).join(" · ")} · {p.project_id.slice(-6)}</option>
          ))}
        </select>}
      </header>
      <nav className="global-nav" aria-label="주요 메뉴"><a aria-current={!route.startsWith('/policy')?'page':undefined} href="#/deploy">배포</a><a aria-current={route.startsWith('/policy')?'page':undefined} href="#/policy/1.1.0/overview">정책</a></nav>
      {route.startsWith('/policy') && <PolicyWorkspace route={route}/>}
      <div hidden={route.startsWith('/policy')}>
      <div className={`shell${project ? " with-side" : ""}`}>
      {project && <History deployments={deployments} selectedId={selected?.id} following={!selectedId} versions={versions}
                           onSelect={(id) => setSelectedId(id === deployments[0]?.id ? null : id)} onFollow={() => setSelectedId(null)}
                           onRollback={(id) => act(api.rollback, id)} />}
      <main className="main">
      {runtime?.analysis_backend === "codex-cli" && <p className="usage">
        로컬 Codex 실제 분석 · {runtime.model} / {runtime.reasoning_effort} · ChatGPT 사용량 사용 · 팀 API 비용 $0
      </p>}
      {runtime?.analysis_backend === "openai" && <p className="usage">
        OpenAI API 실제 분석 · {runtime.model} / {runtime.reasoning_effort} · 팀 API 크레딧 사용
      </p>}
      {runtime?.mapper_mode === "github" && <p className="usage">
        GitHub Repo Mapper 연결됨 · 선택한 커밋의 소스를 직접 가져와 분석합니다.
      </p>}
      {runtime?.aws_enabled && <p className="usage">AWS 실제 배포 연결됨 · 프로젝트별로 앱과 데이터를 분리해 배포합니다.</p>}
      {error && <p className="error banner">{error}</p>}
      {linkedLoading&&<p role="status">연결된 배포의 검사 기록을 불러오는 중입니다.</p>}
      {linkedError&&<p className="error banner" role="alert">{linkedError} <a href="#/deploy">배포 목록으로 돌아가기</a></p>}

      <DeployBar runtime={runtime} project={project} running={running} versions={versions} lastCommit={deployments[0]?.commit_sha}
                 onDeploy={async (body) => {
                   try {
                     const res = await api.deployRepo(body);
                     if (res.project_id !== projectId) choose(res.project_id);
                     setSelectedId(null);
                     refresh();
                   } catch (err) { setError(err.message); }
                 }} />
      {!projects.length && <p className="dim empty-hint">레포와 배포 위치를 고르고 배포를 누르면 아래에 과정과 결과가 나타납니다.</p>}

      {project && (
        <>
          <div className="toolbar">
            <div className="toolbar-main">
              <strong className="repo">{project.repo_url.replace("https://github.com/", "")}</strong>
              <span className="dim"> · {project.branch}</span>
              {selected && <code className="dep-id" title="이 화면이 보여 주는 배포">{selected.id}</code>}
              {selected && selectedId && <button className="small secondary" onClick={() => setSelectedId(null)}>지난 배포 보는 중 · 최신으로</button>}
              {selected && (
                <span className="meta">
                  {TRIGGER[selected.triggered_by]} · <span className="mono">{versionOf(versions, selected.commit_sha)}{selected.commit_sha?.slice(0, 7) ?? "커밋 미정"}</span>
                  {selected.analysis_mode && ` · ${MODE[selected.analysis_mode]}`}
                </span>
              )}
              {selected && <Badge status={selected.status} />}
            </div>
          </div>
          {selected?.change_reasons.length > 0 && (
            <p className="dim reasons">판정 근거: {selected.change_reasons.join(" / ")}</p>
          )}

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

          {selected && (
            <Section n="4" title="코드 변경 · 정책 검사">
              <div className="policy-patch-stack">
              <PolicyCard data={policy} error={policyError} history={policyHistory} deploymentId={selected.id}
                onRecheck={target=>api.policyRecheck(selected.id,target).then(()=>api.policyHistory(selected.id)).then(setPolicyHistory)}
                onReview={(job,digest)=>api.policyReview(selected.id,job,digest).then(()=>api.policyHistory(selected.id)).then(setPolicyHistory)}
                onFailureReview={(execution,body)=>api.policyFailureReview(selected.id,execution,body).then(()=>api.policyHistory(selected.id)).then(setPolicyHistory)} />
              <PatchCard patch={patch} loading={patchLoading} error={patchError} />
              </div>
            </Section>
          )}

        </>
      )}
      </main>
      </div>
      </div>
    </div>
  );
}
