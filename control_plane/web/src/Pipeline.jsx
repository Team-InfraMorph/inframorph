import { describe, explain } from "./explain.js";

export const TARGETS = { local: "Local (노트북 Docker)", aws: "AWS (서울)" };
export const STATUS = {
  CREATED: "대기", DEPLOYING: "배포 중", AWAITING_APPROVAL: "승인 대기", LIVE: "정상",
  FAILED: "실패", ROLLED_BACK: "롤백됨", SUPERSEDED: "건너뜀",
};
export const TERMINAL = ["LIVE", "FAILED", "ROLLED_BACK", "SUPERSEDED"];

// 배포 한 번 = 공통 단계(대상마다 같은 일) → 대상별 배포 → 조종실 직접 확인.
// 이벤트의 step 이름만으로는 '근거 검사'와 '패치 검사'가 둘 다 policy라서, 코드 수정 전후로 나눈다.
const SHARED = [
  ["analyze", "AI 분석"], ["evidence", "근거 검사"], ["approval", "사람 승인"],
  ["patch", "코드 수정"], ["patchcheck", "패치 검사"], ["build", "빌드"],
];
const STAGE_LABEL = { ...Object.fromEntries(SHARED), deploy: "배포", verify: "직접 확인" };
const DEPLOY_STEPS = {
  push: "이미지 업로드", infra: "인프라", start: "실행", health: "상태 확인",
  url: "주소 발급", smoke: "외부 접속 테스트", rollback: "롤백",
};
const MARK = { ok: "✓", fail: "✗", started: "", wait: "!", skip: "–", pending: "" };

function stageOf(e, patched) {
  if (e.step === "analyze" || e.step === "patch" || e.step === "build") return e.step;
  if (e.step === "policy") return patched ? "patchcheck" : "evidence";
  if (e.step === "health" && e.detail?.startsWith("조종실 직접 확인")) return "verify";
  return "deploy";
}

/** 한 대상의 이벤트(seq 순) → {stage: {status, first, last, ms, events}}. */
function byStage(events) {
  const stages = {};
  let patched = false;
  for (const e of events) {
    if (e.step === "patch") patched = true;
    const st = (stages[stageOf(e, patched)] ??= { events: [], status: "started" });
    st.events.push(e);
    st.first ??= e.ts;
    st.last = e.ts;
    if (e.duration_ms != null) st.ms = e.duration_ms;
    if (e.status === "fail") st.status = "fail";
  }
  for (const st of Object.values(stages)) {
    if (st.status !== "fail") st.status = st.events.at(-1).status === "ok" ? "ok" : "started";
    st.ms ??= Date.parse(st.last) - Date.parse(st.first);
  }
  return stages;
}

function seconds(ms) {
  if (ms == null || Number.isNaN(ms)) return null;
  return ms < 1000 ? `${(ms / 1000).toFixed(1)}초` : ms < 60000 ? `${Math.round(ms / 1000)}초` : `${Math.floor(ms / 60000)}분 ${Math.round((ms % 60000) / 1000)}초`;
}

function sharedStage(key, deployment, perTarget) {
  if (key === "approval") {
    if (!deployment.approval_reasons?.length) return null; // 구조 변경이 없으면 승인 단계 자체가 없다
    if (deployment.status === "AWAITING_APPROVAL") return { status: "wait" };
    const patched = perTarget.some((s) => s.patch);
    return { status: deployment.approved_at || patched ? "ok" : deployment.status === "FAILED" ? "fail" : "pending" };
  }
  const found = perTarget.map((s) => s[key]).filter(Boolean);
  if (!found.length) {
    if (key === "analyze" && deployment.analysis_mode === "rebuild_only") return { status: "skip", note: "이전 분석 재사용" };
    if (key === "analyze" && perTarget.some((s) => s.evidence)) return { status: "ok" }; // 근거 검사까지 갔으면 분석은 끝났다
    return { status: TERMINAL.includes(deployment.status) ? "skip" : "pending" };
  }
  const status = found.some((s) => s.status === "fail") ? "fail" : found.every((s) => s.status === "ok") ? "ok" : "started";
  const first = Math.min(...found.map((s) => Date.parse(s.first)));
  const ms = status === "started" ? Date.now() - first : Math.max(...found.map((s) => s.ms));
  const failed = found.find((s) => s.status === "fail");
  return { status, ms, fail: failed?.events.find((e) => e.status === "fail") };
}

export function Badge({ status }) {
  return <span className={`badge s-${status}`}>{STATUS[status] ?? status}</span>;
}

function Explained({ detail }) {
  const known = explain(detail);
  return (
    <div className="explain">
      {known ? (
        <>
          <strong>{known.what}</strong>
          {known.next && <p>{known.next}</p>}
          <details><summary>원문 보기</summary><pre>{detail}</pre></details>
        </>
      ) : <pre>{detail}</pre>}
    </div>
  );
}

/** 위쪽 한 줄: 공통 단계 → (Local ∥ AWS) 배포 → 직접 확인. 지금 단계를 강조하고 걸린 시간을 붙인다. */
function Stepper({ deployment, targets, stages }) {
  const shared = SHARED.map(([key, label]) => [key, label, sharedStage(key, deployment, targets.map((t) => stages[t]))])
    .filter(([, , st]) => st);
  const running = !TERMINAL.includes(deployment.status);
  const current = running ? shared.find(([, , st]) => ["started", "wait"].includes(st.status))?.[0] : null;
  const failed = shared.find(([, , st]) => st.status === "fail");
  return (
    <>
      <ol className="stepper">
        {shared.map(([key, label, st]) => (
          <li key={key} className={`st-${st.status}${key === current ? " current" : ""}`}>
            <span className="dot">{MARK[st.status]}</span>
            <span className="label">{label}</span>
            <span className="time">{st.note ?? (st.status === "wait" ? "대기 중" : seconds(st.ms) ?? "")}</span>
          </li>
        ))}
        <li className={`st-${deployOverall(deployment, targets)} fork`}>
          <span className="dot">{MARK[deployOverall(deployment, targets)]}</span>
          <span className="label">{targets.map((t) => TARGETS[t].split(" ")[0]).join(" ∥ ")} 배포</span>
          <span className="time">동시 실행</span>
        </li>
      </ol>
      {failed?.[2].fail && (
        <div className="card stop">
          <h2>{failed[1]} 단계에서 멈췄습니다 · 배포하지 않았습니다</h2>
          <Explained detail={failed[2].fail.detail} />
        </div>
      )}
    </>
  );
}

function deployOverall(deployment, targets) {
  const states = targets.map((t) => deployment.targets[t]?.status);
  if (states.some((s) => s === "FAILED")) return "fail";
  if (states.length && states.every((s) => s === "LIVE" || s === "ROLLED_BACK")) return "ok";
  return states.some((s) => s === "DEPLOYING") ? "started" : TERMINAL.includes(deployment.status) ? "skip" : "pending";
}

export function Verification({ check, onRecheck }) {
  if (!check) return null;
  const time = new Date(check.checked_at).toLocaleTimeString();
  const text = check.status === "ok" ? `조종실이 직접 접속해 확인 · 응답 ${check.code} · ${(check.ms / 1000).toFixed(2)}초`
    : check.status === "fail" ? `직접 확인 실패 · ${check.detail}` : check.detail;
  return (
    <div className={`verify v-${check.status}`}>
      <span>{check.status === "ok" ? "✓" : check.status === "fail" ? "✗" : "–"} {text}</span>
      <span className="dim"> · {time}</span>
      {check.status !== "skipped" && <button className="small secondary" onClick={onRecheck}>다시 확인</button>}
    </div>
  );
}

/** 시작·완료 두 줄을 한 줄로 합친 대상별 상세 기록. */
function Log({ events }) {
  const rows = [];
  const open = {};
  let patched = false;
  for (const e of events) {
    if (e.step === "patch") patched = true;
    const stage = stageOf(e, patched);
    const label = stage === "deploy" ? DEPLOY_STEPS[e.step] ?? e.step : STAGE_LABEL[stage];
    const key = `${stage}:${e.step}`;
    if (open[key] != null && e.status !== "started") {
      const row = rows[open[key]];
      Object.assign(row, { status: e.status, end: e.ts, detail: e.detail ?? row.detail, ms: e.duration_ms ?? row.ms });
      delete open[key];
    } else {
      rows.push({ seq: e.seq, label, status: e.status, start: e.ts, end: e.ts, detail: e.detail, ms: e.duration_ms });
      if (e.status === "started") open[key] = rows.length - 1;
    }
  }
  return (
    <ol className="timeline">
      {rows.map((r) => (
        <li key={r.seq} className={`ev-${r.status}`}>
          <span className="step">{r.label}</span>
          <span className="mark">{r.status === "ok" ? "완료" : r.status === "fail" ? "실패" : "진행 중"}</span>
          <span className="dim">{seconds(r.ms ?? Date.parse(r.end) - Date.parse(r.start))}</span>
          {r.detail && <div className="detail">{describe(r.detail)}</div>}
        </li>
      ))}
    </ol>
  );
}

const HEADLINE = {
  LIVE: "배포 완료", FAILED: "배포 실패", ROLLED_BACK: "이전 버전으로 복구됨",
  DEPLOYING: "배포 중", CREATED: "대기 중", AWAITING_APPROVAL: "승인 대기",
};

function TargetResult({ target, deployment, stage, events, onRecheck }) {
  const state = deployment.targets[target];
  const status = state?.status ?? "CREATED";
  const deploy = stage.deploy;
  const blocked = !deploy && Object.values(stage).some((s) => s.status === "fail");
  const failure = deploy?.events.find((e) => e.status === "fail") ?? (status === "FAILED" ? stage.verify?.events.find((e) => e.status === "fail") : null);
  const steps = [];
  for (const e of deploy?.events ?? []) {
    const known = steps.find((s) => s.step === e.step);
    if (known) known.status = e.status === "started" && known.status !== "started" ? known.status : e.status;
    else steps.push({ step: e.step, status: e.status });
  }
  const running = status === "DEPLOYING" && deploy?.status === "started";
  return (
    <div className={`card result r-${blocked ? "BLOCKED" : status}`}>
      <div className="row between">
        <h2>{TARGETS[target]}</h2>
        <Badge status={status} />
      </div>
      <p className="headline">
        {blocked ? "앞 단계에서 멈춰 배포하지 않음" : failure && status === "DEPLOYING" ? "배포 실패 · 정리 중" : HEADLINE[status] ?? status}
        {running && <span className="dim"> · {DEPLOY_STEPS[steps.at(-1)?.step] ?? "준비"} {seconds(Date.now() - Date.parse(deploy.first))}</span>}
      </p>
      {state?.url && <a className="url" href={state.url} target="_blank" rel="noreferrer">{state.url}</a>}
      <Verification check={state?.verification} onRecheck={onRecheck} />
      {steps.length > 0 && (
        <ul className="chips">
          {steps.map((s) => (
            <li key={s.step} className={`chip c-${s.status}`}>{MARK[s.status]} {DEPLOY_STEPS[s.step] ?? s.step}</li>
          ))}
        </ul>
      )}
      {failure?.detail && <Explained detail={failure.detail} />}
      {events.length > 0 && (
        <details className="log">
          <summary>상세 기록 ({events.length})</summary>
          <Log events={events} />
        </details>
      )}
    </div>
  );
}

export function Pipeline({ deployment, events, targets, onRecheck }) {
  const perTarget = Object.fromEntries(targets.map((t) => [t, events.filter((e) => e.target === t)]));
  const stages = Object.fromEntries(targets.map((t) => [t, byStage(perTarget[t])]));
  return (
    <>
      <Stepper deployment={deployment} targets={targets} stages={stages} />
      <div className="targets">
        {targets.map((t) => (
          <TargetResult key={t} target={t} deployment={deployment} stage={stages[t]} events={perTarget[t]}
                        onRecheck={onRecheck} />
        ))}
      </div>
    </>
  );
}
