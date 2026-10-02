import { useEffect, useState } from "react";
import { describe, explain } from "./explain.js";

export const TARGETS = { local: "Local (노트북 Docker)", aws: "AWS (서울)" };
export const STATUS = {
  CREATED: "대기", DEPLOYING: "배포 중", AWAITING_APPROVAL: "승인 대기", LIVE: "정상",
  FAILED: "실패", ROLLED_BACK: "롤백됨", SUPERSEDED: "건너뜀",
};
export const TERMINAL = ["LIVE", "FAILED", "ROLLED_BACK", "SUPERSEDED"];
const SHORT = { local: "Local", aws: "AWS" };

// 배포 한 번 = 공통 단계(대상마다 같은 일) → 대상별 배포 레인 → 조종실 직접 확인.
// 이벤트의 step 이름만으로는 '근거 검사'와 '패치 검사'가 둘 다 policy라서, 코드 수정 전후로 나눈다.
const SHARED = [
  ["analyze", "AI 분석"], ["evidence", "근거 검사"], ["approval", "사람 승인"],
  ["patch", "코드 수정"], ["patchcheck", "패치 검사"], ["build", "빌드"],
];
const STAGE_LABEL = { ...Object.fromEntries(SHARED), deploy: "배포", verify: "직접 확인" };
const DEPLOY_STEPS = {
  plan: "변경 미리보기", push: "이미지 업로드", infra: "인프라", start: "실행", health: "상태 확인",
  url: "주소 발급", smoke: "외부 접속 테스트", rollback: "롤백",
};
// 대상별 배포 단계의 기본 순서. 배포기가 이 밖의 단계를 보내면 뒤에 붙인다.
const LANE = { aws: ["plan", "push", "infra", "start", "health", "url", "smoke"], local: ["start", "health", "url", "smoke"] };
const MARK = { ok: "✓", fail: "✗", wait: "!", skip: "–", notrun: "" };
const HEADLINE = {
  LIVE: "배포 완료", FAILED: "배포 실패", ROLLED_BACK: "이전 버전으로 복구됨",
  DEPLOYING: "배포 중", CREATED: "대기 중", AWAITING_APPROVAL: "승인 대기",
};

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

function clock(ms) {
  if (ms == null || Number.isNaN(ms) || ms < 0) return "";
  const s = Math.floor(ms / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** 진행 중일 때만 1초마다 다시 그려 경과 시간이 흐르게 한다. */
function useNow(active) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!active) return undefined;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [active]);
  return now;
}

function sharedStage(key, deployment, perTarget, now) {
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
  const ms = status === "started" ? now - first : Math.max(...found.map((s) => s.ms));
  const failed = found.find((s) => s.status === "fail");
  // 조종실이 '생략'·'미연결'을 남긴 단계는 ✓로 보이지 않게 그 문구를 그대로 쓴다.
  const said = found.flatMap((s) => s.events).map((e) => e.detail).find((d) => d && /생략|미연결/.test(d));
  if (status === "ok" && said) return { status: /생략/.test(said) ? "skip" : "ok", ms, note: said };
  return { status, ms, fail: failed?.events.find((e) => e.status === "fail") };
}

/** 단계가 남긴 결과 한 줄. 화면에 이미 있는 분석·수정·설계도 데이터만 쓴다(추측하지 않는다). */
function stageNote(key, st, ctx) {
  if (st.note) return st.note;
  if (st.status === "skip") return "실행 안 함";
  if (key === "approval") return st.status === "wait" ? "구조 변경 · 사람 확인 필요" : st.status === "ok" ? "승인됨" : null;
  if (st.status !== "ok") return null;
  if (key === "analyze") {
    const i = ctx.intent;
    return i ? `서비스 ${i.workloads.length} · 저장 ${i.state.length} · 비밀값 ${i.secrets.length}` : null;
  }
  if (key === "evidence" && ctx.intent) {
    const n = [...ctx.intent.workloads, ...ctx.intent.state].reduce((sum, e) => sum + e.evidence.length, 0);
    return `근거 ${n}곳 모두 실제 코드에 있음`;
  }
  if (key === "patch") {
    const p = Object.values(ctx.patch)[0];
    if (!p) return null;
    if (p.status === "unchanged") return "고칠 것 없음 · 원본 그대로";
    const added = p.files.filter((f) => f.action === "add").length;
    return `파일 ${p.files.length}개 (추가 ${added} · 수정 ${p.files.length - added})`;
  }
  if (key === "patchcheck") return "허용 범위 안 · 위변조 없음";
  if (key === "build") {
    const tag = Object.values(ctx.plans)[0]?.image_tag;
    return tag ? `새 이미지 ${tag.slice(0, 11)}` : null;
  }
  return null;
}

/** 대상 하나의 배포 단계들 [{step, label, status, ms}]. 실패한 뒤의 단계는 'notrun'(실행 안 됨). */
function lane(target, deploy, running, live, now) {
  const seen = {};
  for (const e of deploy?.events ?? []) {
    const cur = (seen[e.step] ??= { first: e.ts });
    cur.status = e.status === "started" && cur.status && cur.status !== "started" ? cur.status : e.status;
    cur.last = e.ts;
    if (e.duration_ms != null) cur.ms = e.duration_ms;
  }
  const base = LANE[target] ?? [];
  const order = [...base, ...Object.keys(seen).filter((s) => !base.includes(s))];
  const lastSeen = Math.max(-1, ...order.map((step, i) => (seen[step] ? i : -1)));
  let stopped = false;
  return order.map((step, i) => {
    const got = seen[step];
    // 뒤 단계가 이미 왔는데 이 단계 기록이 없으면 배포기가 이 단계를 하지 않은 것(–)이다.
    let status = got?.status ?? (i < lastSeen ? "skip" : stopped || !running ? (live ? "skip" : "notrun") : "pending");
    if (status === "started" && !running) status = "notrun";
    if (status === "fail") stopped = true;
    const ms = got && (got.status === "started" ? now - Date.parse(got.first) : got.ms ?? Date.parse(got.last) - Date.parse(got.first));
    return { step, label: DEPLOY_STEPS[step] ?? step, status, ms };
  });
}

export function Badge({ status }) {
  return <span className={`badge s-${status}`}>{STATUS[status] ?? status}</span>;
}

function Explained({ detail }) {
  const known = explain(detail);
  return known ? (
    <div className="explain">
      <strong>{known.what}</strong>
      {known.next && <p>{known.next}</p>}
      <details><summary>원문 보기</summary><pre>{detail}</pre></details>
    </div>
  ) : <div className="explain"><pre>{describe(detail)}</pre></div>;
}

/** 실패 위치(어느 대상의 어느 단계) + 무슨 일인지 + 다음에 할 일. */
function Where({ place, detail }) {
  return (
    <div className="where">
      <div className="where-head"><span className="where-label">실패 위치</span><strong>{place}</strong></div>
      <Explained detail={detail} />
    </div>
  );
}

function Node({ status, label, time, note, flag }) {
  return (
    <li className={`node n-${status}`}>
      <span className="dot">{MARK[status] ?? ""}</span>
      <span className="label">{label}</span>
      <span className="time">{time}</span>
      {note && <span className="note">{note}</span>}
      {flag && <span className="flag">{flag}</span>}
    </li>
  );
}

const nodeTime = (status, ms) => (status === "started" ? clock(ms) : ["ok", "fail"].includes(status) ? seconds(ms) : status === "wait" ? "대기 중" : "");

function nextUp(targets, lanes) {
  const next = targets.map((t) => [t, lanes[t].find((n) => n.status === "pending")]).filter(([, n]) => n);
  return next.length ? `다음: ${next.map(([t, n]) => `${SHORT[t]} › ${n.label}`).join("   ·   ")} 준비 중` : "다음 단계 준비 중";
}

/** 맨 위 한 줄: 지금 무엇을 하는지, 또는 어떻게 끝났는지. */
function NowBar({ deployment, shared, lanes, targets, now, started }) {
  const done = TERMINAL.includes(deployment.status);
  const end = done ? Date.parse(deployment.updated_at ?? "") : now;
  let tone = "run";
  let text;
  if (deployment.status === "AWAITING_APPROVAL") {
    tone = "wait";
    text = "사람 승인을 기다리는 중 · 인프라 구조가 바뀌는 배포입니다";
  } else if (deployment.status === "CREATED") {
    tone = "idle";
    text = "배포 대기 중";
  } else if (!done) {
    const sharedNow = shared.find(([, , st]) => st.status === "started");
    const laneNow = targets.map((t) => [t, lanes[t].find((n) => n.status === "started")]).filter(([, n]) => n);
    text = sharedNow ? `지금: ${sharedNow[1]} 중`
      : laneNow.length ? `지금: ${laneNow.map(([t, n]) => `${SHORT[t]} › ${n.label}`).join("   ·   ")}`
      : nextUp(targets, lanes);
  } else {
    const sharedFail = shared.find(([, , st]) => st.status === "fail");
    const fails = targets.filter((t) => deployment.targets[t]?.status === "FAILED");
    if (sharedFail) {
      tone = "fail";
      text = `실패: 공통 › ${sharedFail[1]} 단계에서 멈춤 · 배포하지 않음`;
    } else if (fails.length) {
      tone = "fail";
      const where = fails.map((t) => `${SHORT[t]} › ${lanes[t].find((n) => n.status === "fail")?.label ?? "배포"}`);
      const fine = targets.filter((t) => deployment.targets[t]?.status === "LIVE").map((t) => SHORT[t]);
      text = `실패: ${where.join(", ")}${fine.length ? `   (${fine.join(", ")}는 완료)` : ""}`;
    } else if (deployment.status === "LIVE") {
      tone = "ok";
      text = "모든 대상 배포 완료";
    } else {
      tone = "idle";
      text = STATUS[deployment.status] ?? deployment.status;
    }
  }
  const total = started != null && !Number.isNaN(end) ? clock(end - started) : "";
  return (
    <div className={`nowbar t-${tone}`}>
      <span className="pulse" />
      <strong>{text}</strong>
      {total && <span className="elapsed">{done ? "총 " : ""}{total}</span>}
    </div>
  );
}

/** 배포 흐름: 공통 단계 트랙 → 대상별 레인. 진행 중인 칸은 움직이고, 실패 칸에는 '여기서 멈춤'. */
function Flow({ deployment, targets, stages, ctx, now }) {
  const running = !TERMINAL.includes(deployment.status);
  const shared = SHARED.map(([key, label]) => [key, label, sharedStage(key, deployment, targets.map((t) => stages[t]), now)])
    .filter(([, , st]) => st);
  const stop = shared.findIndex(([, , st]) => st.status === "fail");
  const lanes = Object.fromEntries(targets.map((t) => {
    const status = deployment.targets[t]?.status;
    const laneRunning = running && !["LIVE", "FAILED", "ROLLED_BACK"].includes(status);
    const nodes = lane(t, stages[t].deploy, laneRunning, status === "LIVE", now);
    // 배포기가 하지 않은 단계(–)는 레인에서 뺀다. 진행 중에는 앞으로 할 단계를 회색으로 보여 준다.
    const shown = nodes.filter((n) => n.status !== "skip");
    return [t, stop >= 0 ? shown.map((n) => ({ ...n, status: "notrun" })) : shown];
  }));
  return (
    <div className="card flow">
      <NowBar deployment={deployment} shared={shared} lanes={lanes} targets={targets} now={now}
              started={Date.parse(deployment.created_at)} />
      <div className="flow-body">
        <div className="lane-title">공통</div>
        <ol className="track">
          {shared.map(([key, label, st], i) => {
            const status = stop >= 0 && i > stop ? "notrun" : st.status;
            return <Node key={key} label={label} status={status} time={nodeTime(status, st.ms)}
                         note={status === "notrun" ? null : stageNote(key, st, ctx)} flag={i === stop ? "여기서 멈춤" : null} />;
          })}
        </ol>
        {targets.map((t) => (
          <div key={t} className="lane-row">
            <div className="lane-title">{SHORT[t]} <Badge status={deployment.targets[t]?.status ?? "CREATED"} /></div>
            <ol className="track small">
              {lanes[t].map((n) => (
                <Node key={n.step} label={n.label} status={n.status} time={nodeTime(n.status, n.ms)}
                      flag={n.status === "fail" ? "여기서 멈춤" : null} />
              ))}
            </ol>
          </div>
        ))}
      </div>
      {stop >= 0 && shared[stop][2].fail && <Where place={`공통 › ${shared[stop][1]}`} detail={shared[stop][2].fail.detail} />}
    </div>
  );
}

const AWS_MODE = {
  first: "처음 배포 · 새 앱과 새 주소를 만듦",
  resume: "이전에 멈춘 첫 배포를 이어서 · 새 주소",
  redeploy: "기존 앱 갱신 · 주소는 그대로",
};

function json(text) {
  try { return JSON.parse(text); } catch { return null; }
}

/** 대상별 '바뀐 것 / 그대로인 것'. 배포기가 알려 준 사실만 쓰고, 모르면 줄을 만들지 않는다. */
function changes(target, deploy, failure) {
  if (target !== "aws") return []; // Local 배포기는 아직 바뀐 것 요약을 남기지 않는다
  const rows = [];
  let mode = null, applied = null, preview = null;
  for (const e of deploy?.events ?? []) {
    const d = json(e.detail);
    if (d?.mode) mode = d;
    if (d?.stage_plan) applied = d.stage_plan; // Terraform apply 결과
    if (d?.changes) preview = d.changes; // apply 전 Terraform plan 미리보기
  }
  const count = (c) => {
    const parts = [["추가", c.create], ["변경", c.update], ["교체", c.replace], ["삭제", c.delete]]
      .filter(([, n]) => n).map(([w, n]) => `${w} ${n}`);
    return parts.length ? parts.join(" · ") : "바뀐 것 없음 (그대로)";
  };
  if (mode) rows.push(["배포 방식", AWS_MODE[mode.mode] ?? mode.mode]);
  if (deploy?.events.some((e) => e.step === "push" && e.status === "ok")) rows.push(["이미지", "새 이미지를 저장소(ECR)에 올림"]);
  if (applied) rows.push(["클라우드 자원", count(applied)]);
  else if (preview) rows.push(["클라우드 자원", `예정: ${count(preview)}${failure ? " · 반영 완료 전에 멈춤" : ""}`]);
  else if (failure?.step === "infra") rows.push(["클라우드 자원", "반영 완료 전에 멈춤"]);
  else if (failure) rows.push(["클라우드 자원", "건드리지 않음 (적용 전에 멈춤)"]);
  return rows;
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

function TargetResult({ target, deployment, stage, onRecheck }) {
  const state = deployment.targets[target];
  const status = state?.status ?? "CREATED";
  const deploy = stage.deploy;
  const blocked = !deploy && Object.values(stage).some((s) => s.status === "fail");
  const failure = deploy?.events.find((e) => e.status === "fail") ?? (status === "FAILED" ? stage.verify?.events.find((e) => e.status === "fail") : null);
  const rows = changes(target, deploy, failure);
  return (
    <div className={`card result r-${blocked ? "BLOCKED" : status}`}>
      <div className="row between">
        <h2>{TARGETS[target]}</h2>
        <Badge status={status} />
      </div>
      <p className="headline">
        {blocked ? "앞 단계에서 멈춰 배포하지 않음" : failure && status === "DEPLOYING" ? "배포 실패 · 정리 중" : HEADLINE[status] ?? status}
      </p>
      {state?.url && <a className="url" href={state.url} target="_blank" rel="noreferrer">{state.url}</a>}
      <Verification check={state?.verification} onRecheck={onRecheck} />
      {rows.length > 0 && (
        <table className="changes"><tbody>
          {rows.map(([k, v]) => <tr key={k}><th>{k}</th><td>{v}</td></tr>)}
        </tbody></table>
      )}
      {failure?.detail && <Where place={`${SHORT[target]} › ${DEPLOY_STEPS[failure.step] ?? STAGE_LABEL[failure.step] ?? failure.step}`} detail={failure.detail} />}
    </div>
  );
}

/** 시작·완료 두 줄을 한 줄로 합친 행. */
function mergedRows(events) {
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
      Object.assign(row, { seq: e.seq, status: e.status, end: e.ts, detail: e.detail ?? row.detail });
      delete open[key];
    } else {
      rows.push({ seq: e.seq, label, status: e.status, start: e.ts, end: e.ts, detail: e.detail });
      if (e.status === "started") open[key] = rows.length - 1;
    }
  }
  return rows;
}

/** 실시간 기록: 모든 대상의 단계를 최신순으로. 새 줄은 살짝 떠오른다. */
export function LiveFeed({ events, running }) {
  const rows = Object.keys(TARGETS)
    .flatMap((t) => mergedRows(events.filter((e) => e.target === t)).map((r) => ({ ...r, target: t })))
    .sort((a, b) => b.seq - a.seq);
  return (
    <div className="card feed">
      <div className="row between">
        <h2>실시간 기록</h2>
        {running && <span className="live"><span className="pulse" />LIVE</span>}
      </div>
      {!rows.length && <p className="dim">아직 기록이 없습니다</p>}
      <ol>
        {rows.map((r) => (
          <li key={`${r.target}-${r.start}-${r.label}`} className={`f-${r.status}`}>
            <span className={`tag tag-${r.target}`}>{SHORT[r.target]}</span>
            <span className="what"><strong>{r.label}</strong> {r.status === "ok" ? "완료" : r.status === "fail" ? "실패" : "진행 중"}</span>
            <span className="at">{new Date(r.end).toLocaleTimeString()}</span>
            {r.detail && <span className="detail">{explain(r.detail)?.what ?? describe(r.detail)}</span>}
          </li>
        ))}
      </ol>
    </div>
  );
}

export function Pipeline({ deployment, events, targets, ctx }) {
  const now = useNow(!TERMINAL.includes(deployment.status));
  const stages = Object.fromEntries(targets.map((t) => [t, byStage(events.filter((e) => e.target === t))]));
  return <Flow deployment={deployment} targets={targets} stages={stages} ctx={ctx} now={now} />;
}

export function Results({ deployment, events, targets, onRecheck }) {
  return (
    <div className="targets">
      {targets.map((t) => (
        <TargetResult key={t} target={t} deployment={deployment} stage={byStage(events.filter((e) => e.target === t))}
                      onRecheck={onRecheck} />
      ))}
    </div>
  );
}
