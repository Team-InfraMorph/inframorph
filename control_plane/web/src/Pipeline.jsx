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
  plan: "변경 미리보기", build: "이미지 확인", push: "이미지 업로드", infra: "인프라", start: "실행", health: "상태 확인",
  url: "주소 발급", smoke: "외부 접속 테스트", rollback: "롤백",
};
// 대상별 배포 단계의 기본 순서. 배포기가 이 밖의 단계를 보내면 뒤에 붙인다.
const LANE = { aws: ["plan", "push", "infra", "start", "health", "url", "smoke"], local: ["start", "health", "url", "smoke"] };
const MARK = { ok: "✓", fail: "✗", wait: "!", skip: "–", notrun: "" };
const HEADLINE = {
  LIVE: "배포 완료", FAILED: "배포 실패", ROLLED_BACK: "이전 버전으로 복구됨",
  DEPLOYING: "배포 중", CREATED: "대기 중", AWAITING_APPROVAL: "승인 대기",
};

function stageOf(e, patched, planned) {
  if (e.step === "build" && planned) return "deploy"; // AWS Adapter가 변경 미리보기 뒤에 하는 이미지 확인
  if (e.step === "analyze" || e.step === "patch" || e.step === "build") return e.step;
  if (e.step === "policy") return patched ? "patchcheck" : "evidence";
  if (e.step === "health" && e.detail?.startsWith("조종실 직접 확인")) return "verify";
  return "deploy";
}

/** 한 대상의 이벤트(seq 순) → {stage: {status, first, last, ms, events}}. */
function byStage(events) {
  const stages = {};
  let patched = false, planned = false;
  for (const e of events) {
    if (e.step === "patch") patched = true;
    if (e.step === "plan") planned = true;
    const st = (stages[stageOf(e, patched, planned)] ??= { events: [], status: "started" });
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

const GAP_MS = 15000;

/** 대상 하나의 배포 단계들(실제 일어난 순서). 실패 뒤 단계는 'notrun'.
 * AWS Adapter는 Plan에 DB가 있을 때만, 인프라 적용 직후 DB 준비(bootstrap·migration)를 기록 없이 실행한다
 * (adapters/aws/deployment.py `if self.plan.db is not None: self._prepare_database`). 그 경우에만 'DB 준비' 칸을 넣고,
 * 그 밖의 기록 없는 15초 이상 구간은 칸을 만들지 않고 앞 칸의 선 위에 시간만 적는다. */
function lane(target, events, running, now, hasDb) {
  const seen = {};
  const order = [];
  let patched = false, planned = false, prev = null;
  for (const e of events) {
    if (e.step === "patch") patched = true;
    if (e.step === "plan") planned = true;
    if (stageOf(e, patched, planned) === "deploy") {
      let cur = seen[e.step];
      if (!cur) {
        cur = seen[e.step] = { step: e.step, first: e.ts, from: prev ?? e.ts, events: [] };
        order.push(cur);
      }
      cur.status = e.status === "started" && cur.status && cur.status !== "started" ? cur.status : e.status;
      cur.last = e.ts;
      cur.started ||= e.status === "started";
      cur.events.push(e);
    }
    prev = e.ts;
  }
  const nodes = [];
  let stopped = false;
  let prevEnd = -Infinity;
  order.forEach((cur, i) => {
    const later = i < order.length - 1;
    let status = cur.status;
    if (status === "started" && (later || !running)) status = later ? "ok" : "notrun"; // 다음 단계가 왔으면 끝난 것
    if (status === "fail") stopped = true;
    // '완료'만 보내는 단계는 직전 기록부터 이 단계 완료까지를 이 단계 시간으로 본다.
    // 앞 칸이 이미 쓴 시간은 다시 세지 않는다.
    const start = cur.started ? Date.parse(cur.first) : Math.max(Date.parse(cur.from), prevEnd);
    const end = status === "started" ? now : Date.parse(later && cur.status === "started" ? order[i + 1].first : cur.last);
    const ms = end - start;
    prevEnd = end;
    const prior = nodes.at(-1);
    const dbSlot = hasDb && target === "aws" && i > 0 && order[i - 1].step === "infra" && cur.step === "start";
    const gap = prior && cur.started ? Date.parse(cur.first) - Date.parse(order[i - 1].last) : 0;
    if (dbSlot && gap > 0) nodes.push({ step: "db", label: "DB 준비", status: "ok", ms: gap, gap: "db" });
    else if (gap > GAP_MS) prior.pause = gap;
    nodes.push({ step: cur.step, label: DEPLOY_STEPS[cur.step] ?? cur.step, status, ms, events: cur.events });
  });
  for (const step of LANE[target] ?? []) {
    if (!seen[step] && running && !stopped) nodes.push({ step, label: DEPLOY_STEPS[step] ?? step, status: "pending" });
    else if (!seen[step] && stopped && order.length) nodes.push({ step, label: DEPLOY_STEPS[step] ?? step, status: "notrun" });
  }
  // 인프라가 끝났고 아직 '실행'이 시작되지 않았으면 지금 DB 준비 중이다.
  const last = order.at(-1);
  if (running && hasDb && target === "aws" && last?.step === "infra" && last.status === "ok") {
    nodes.splice(nodes.findIndex((n) => n.step === "infra") + 1, 0,
      { step: "db", label: "DB 준비", status: "started", ms: now - Date.parse(last.last), gap: "db" });
  }
  // 진행 중인데 다음 단계의 '시작'이 아직 안 왔으면, 마지막으로 끝난 칸에서 다음 칸으로 빛줄기를 흘린다.
  const next = nodes.findIndex((n) => n.status === "pending");
  if (running && next > 0 && !nodes.some((n) => n.status === "started")) nodes[next - 1].flowing = true;
  return nodes;
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

function Node({ status, label, time, note, flag, onClick, selected, index, flowing, pause }) {
  return (
    <li className={`node n-${status}${flowing ? " flowing" : ""}${onClick ? " clickable" : ""}${selected ? " selected" : ""}`} onClick={onClick ?? undefined}
        role={onClick ? "button" : undefined} tabIndex={onClick ? 0 : undefined}
        onKeyDown={onClick ? (e) => (e.key === "Enter" || e.key === " ") && onClick() : undefined}>
      <span className="link"><i /></span>
      {pause && <span className="link-note">기록 없음 {seconds(pause)}</span>}
      <span className="dot">{MARK[status] || (status === "pending" || status === "notrun" ? index : "")}</span>
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
function Flow({ deployment, targets, stages, perTarget, ctx, now }) {
  const [open, setOpen] = useState(null);
  const running = !TERMINAL.includes(deployment.status);
  const shared = SHARED.map(([key, label]) => [key, label, sharedStage(key, deployment, targets.map((t) => stages[t]), now)])
    .filter(([, , st]) => st);
  const stop = shared.findIndex(([, , st]) => st.status === "fail");
  const lanes = Object.fromEntries(targets.map((t) => {
    const status = deployment.targets[t]?.status;
    const laneRunning = running && !["LIVE", "FAILED", "ROLLED_BACK"].includes(status);
    const nodes = lane(t, perTarget[t], laneRunning, now, Boolean(ctx.plans?.[t]?.db));
    return [t, stop >= 0 ? nodes.map((n) => ({ ...n, status: "notrun" })) : nodes];
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
            const id = `shared:${key}`;
            const evs = targets.flatMap((t) => stages[t][key]?.events ?? []);
            return <Node key={key} index={i + 1} label={label} status={status} time={nodeTime(status, st.ms)}
                         note={status === "notrun" ? null : stageNote(key, st, ctx)} flag={i === stop ? "여기서 멈춤" : null}
                         selected={open?.id === id} onClick={evs.length ? () => setOpen(open?.id === id ? null : { id, title: `공통 › ${label}`, events: evs }) : null} />;
          })}
        </ol>
        {targets.map((t) => (
          <div key={t} className="lane-row">
            <div className="lane-title">{SHORT[t]} <Badge status={deployment.targets[t]?.status ?? "CREATED"} /></div>
            <ol className="track small">
              {lanes[t].map((n, i) => (
                <Node key={n.step} index={i + 1} label={n.label} status={n.status} flowing={n.flowing} pause={n.pause} time={nodeTime(n.status, n.ms)}
                      flag={n.status === "fail" ? "여기서 멈춤" : null} selected={open?.id === `${t}:${n.step}`}
                      onClick={n.events || n.gap ? () => setOpen(open?.id === `${t}:${n.step}` ? null
                        : { id: `${t}:${n.step}`, title: `${SHORT[t]} › ${n.label}`, events: n.events ?? [], gap: n.gap, ms: n.ms }) : null} />
              ))}
            </ol>
          </div>
        ))}
      </div>
      {open && <StepDetail item={open} onClose={() => setOpen(null)} />}
      {stop >= 0 && shared[stop][2].fail && <Where place={`공통 › ${shared[stop][1]}`} detail={shared[stop][2].fail.detail} />}
    </div>
  );
}

const DETAIL_KEY = {
  tag: "이미지 태그", digest: "고정된 버전(digest)", mode: "배포 방식", reason: "이유", url: "주소", services: "실행한 서비스",
  changes: "자원 변경 예정", stage_plan: "자원 변경", app: "AWS 앱 이름", source_revision: "커밋", code: "단계 코드",
};

function detailRows(detail) {
  const d = json(detail);
  if (!d || typeof d !== "object") return null;
  return Object.entries(d).map(([k, v]) => [DETAIL_KEY[k] ?? k,
    v && typeof v === "object" ? Object.entries(v).map(([a, b]) => `${a} ${typeof b === "object" ? JSON.stringify(b) : b}`).join(" · ") : String(v)]);
}

/** 칸을 누르면 그 단계에서 배포기가 남긴 기록을 시간순으로 보여 준다. */
function StepDetail({ item, onClose }) {
  return (
    <div className="step-detail">
      <div className="row between">
        <strong>{item.title}</strong>
        <button className="small secondary" onClick={onClose}>닫기</button>
      </div>
      {item.gap === "db" && (
        <p className="detail-note">
          AWS Adapter는 앱에 DB가 있으면 인프라 적용 직후 DB 준비(앱 전용 DB·계정 생성 → 테이블 생성, 일회성 ECS 작업 2개)를 실행합니다.
          이 단계는 시작·완료 기록을 따로 보내지 않아, 시간은 '인프라 완료 ~ 실행 시작' 사이로 잽니다 (adapters/aws/deployment.py).
        </p>
      )}
      <ol>
        {item.events.map((e) => (
          <li key={e.seq} className={`f-${e.status}`}>
            <span className="at">{new Date(e.ts).toLocaleTimeString()}</span>
            <span className={`tag tag-${e.target}`}>{SHORT[e.target]}</span>
            <span>{e.status === "ok" ? "완료" : e.status === "fail" ? "실패" : "시작"}</span>
            {e.detail && (detailRows(e.detail) ? (
              <table className="changes"><tbody>
                {detailRows(e.detail).map(([k, v]) => <tr key={k}><th>{k}</th><td className="mono">{v}</td></tr>)}
              </tbody></table>
            ) : <span className="detail">{explain(e.detail)?.what ?? describe(e.detail)}</span>)}
          </li>
        ))}
      </ol>
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
  // 배포기 보고(LIVE)와 조종실 직접 확인을 따로 본다. 둘이 다르면 '정상'이라고 말하지 않는다.
  const check = state?.verification?.status;
  const unverified = status === "LIVE" && check === "fail";
  const fake = check === "skipped";
  return (
    <div className={`card result r-${blocked ? "BLOCKED" : unverified ? "UNVERIFIED" : status}`}>
      <div className="row between">
        <h2>{TARGETS[target]}</h2>
        <Badge status={status} />
      </div>
      <p className="headline">
        {blocked ? "앞 단계에서 멈춰 배포하지 않음" : failure && status === "DEPLOYING" ? "배포 실패 · 정리 중"
          : unverified ? "배포기는 완료라고 했지만 접속이 안 됩니다" : HEADLINE[status] ?? status}
        {fake && <span className="fake">시험용 가짜 배포 · 실제 아님</span>}
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

export function Pipeline({ deployment, events, targets, ctx }) {
  const now = useNow(!TERMINAL.includes(deployment.status));
  const stages = Object.fromEntries(targets.map((t) => [t, byStage(events.filter((e) => e.target === t))]));
  const perTarget = Object.fromEntries(targets.map((t) => [t, events.filter((e) => e.target === t)]));
  return <Flow deployment={deployment} targets={targets} stages={stages} perTarget={perTarget} ctx={ctx} now={now} />;
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
