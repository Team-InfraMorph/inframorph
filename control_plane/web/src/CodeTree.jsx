// 배포할 앱 한 장: 왼쪽 = 코드 트리(B repo_map.tree), 오른쪽 = '앱이 필요로 하는 것 → 대상별로 무엇이 되나'.
// 필요(근거 줄)는 C Analyzer의 intent, 대상별 구성은 B Planner의 plan, 바뀐 파일은 C Code Patch 결과에서 온다.

const TARGET = { local: "Local", aws: "AWS" };
const DB = { postgres_container: "PostgreSQL 컨테이너", rds_postgres: "RDS PostgreSQL" };
const STORAGE = { volume: "Docker 볼륨", s3: "S3 버킷" };
const LOGS = { docker: "Docker 로그", cloudwatch: "CloudWatch" };
// 비밀값을 넣는 곳은 Plan이 아니라 각 배포기가 정한다: E Local Adapter = app.env, A AWS Adapter = Secrets Manager
const SECRET_STORE = { local: "app.env 파일", aws: "Secrets Manager" };

/** "src/server.js:22", "src/server.js:87" → "server.js:22·87" */
function cite(evidence = []) {
  const lines = {};
  for (const [file, line] of evidence.map((e) => e.split(":"))) (lines[file.split("/").at(-1)] ??= []).push(line);
  return Object.entries(lines).map(([file, ls]) => `${file}:${ls.join("·")}`).join(", ");
}

/** 파일 경로 → 짧은 역할 이름(AI가 근거로 든 파일만). */
function roleOf(intent) {
  const role = {};
  for (const w of intent?.workloads ?? []) for (const e of w.evidence) role[e.split(":")[0]] = w.kind === "http" ? "웹 서버" : "백그라운드";
  for (const s of intent?.state ?? []) {
    for (const e of s.evidence) role[e.split(":")[0]] ??= s.kind === "relational_db" ? "DB" : "파일 저장";
  }
  return role;
}

function build(paths) {
  const root = { dirs: {}, files: [] };
  for (const path of paths) {
    const parts = path.split("/");
    let node = root;
    for (const dir of parts.slice(0, -1)) node = node.dirs[dir] ??= { dirs: {}, files: [] };
    node.files.push(path);
  }
  return root;
}

function Branch({ node, role, change, depth = 0 }) {
  return (
    <ul className={depth ? "tree-sub" : "tree"}>
      {Object.entries(node.dirs).sort(([a], [b]) => a.localeCompare(b)).map(([name, child]) => (
        <li key={name}><span className="tree-dir">{name}/</span><Branch node={child} role={role} change={change} depth={depth + 1} /></li>
      ))}
      {node.files.sort().map((path) => (
        <li key={path} className={`tree-file${role[path] ? " has-role" : ""}`}>
          <span className="tree-name">{path.split("/").at(-1)}</span>
          {role[path] && <span className="tree-role">{role[path]}</span>}
          {change[path] && <span className={`tree-git g-${change[path]}`} title={change[path] === "A" ? "패치로 추가" : "패치로 수정"}>{change[path]}</span>}
        </li>
      ))}
    </ul>
  );
}

function CodeTree({ repoMap, intent, patch }) {
  const changed = Object.values(patch ?? {})[0];
  const change = Object.fromEntries((changed?.files ?? []).map((f) => [f.path, f.action === "add" ? "A" : "M"]));
  const paths = [...new Set([...repoMap.tree, ...Object.keys(change)])];
  const hidden = paths.filter((p) => p.split("/").some((part) => part.startsWith(".")));
  return (
    <div className="codetree">
      <Branch node={build(paths.filter((p) => !hidden.includes(p)))} role={roleOf(intent)} change={change} />
      <p className="tree-legend">
        <span className="tree-git g-A">A</span> 패치로 추가 <span className="tree-git g-M">M</span> 패치로 수정
        {hidden.length > 0 && <> · 숨김·설정 파일 {hidden.length}개 생략</>}
      </p>
    </div>
  );
}

const NAME = { sqlite: "SQLite", postgres: "PostgreSQL", postgresql: "PostgreSQL", mysql: "MySQL", prisma: "Prisma" };
const spread = (n) => (n === 1 ? [50] : n === 2 ? [28, 72] : Array.from({ length: n }, (_, i) => 15 + (70 * i) / (n - 1)));

/** 부품 하나. 아래에 그 부품이 Local·AWS에서 각각 무엇이 되는지 붙인다. */
function Part({ x, y, title, sub, cite, note, env, tone }) {
  return (
    <div className={`apart${tone ? ` ap-${tone}` : ""}`} style={{ left: `${x}%`, top: `${y}%` }}>
      <div className="ap-title">{title}</div>
      {sub && <div className="ap-sub">{sub}</div>}
      {cite && <code className="ap-cite">{cite}</code>}
      {note && <div className="ap-note">{note}</div>}
      {env && (
        <div className="ap-env">
          {Object.entries(env).map(([t, v]) => <span key={t} className={`ape ape-${t}`}><b>{TARGET[t]}</b>{v}</span>)}
        </div>
      )}
    </div>
  );
}

/** 배포할 앱의 구조도: 사용자 → 서비스(웹·백그라운드) → 저장소(DB·파일). AI가 찾은 근거와, 부품마다 대상별 모습을 함께 그린다. */
function AppArch({ intent, plans, targets, repoMap }) {
  const shown = targets.filter((t) => plans[t]);
  if (!intent || !shown.length) return null;
  const plan = plans[shown[0]];
  const envOf = (fn) => Object.fromEntries(shown.map((t) => [t, fn(plans[t], t)]));
  const svc = intent.workloads.map((w, i, all) => ({ ...w, x: spread(all.length)[i], y: 43 }));
  const store = intent.state.map((st, i, all) => ({ ...st, key: st.kind === "relational_db" ? "db" : "storage", x: spread(all.length)[i], y: 76 }));
  const at = Object.fromEntries([...svc.map((w) => [w.name, w]), ...store.map((st) => [st.key, st])]);
  // 연결: Planner가 낸 plan.mermaid(web --> db 등). 없으면 웹 서비스가 모든 저장소에 닿는다고 본다.
  const pairs = plan.mermaid?.split("\n").map((l) => l.trim().split(/\s*-->\s*/)).filter((p) => p.length === 2 && at[p[0]] && at[p[1]])
    ?? svc.flatMap((w) => store.map((st) => [w.name, st.key]));
  const user = { x: 50, y: 12 };
  const curve = (a, b) => `M${a.x},${a.y} C${a.x},${(a.y + b.y) / 2} ${b.x},${(a.y + b.y) / 2} ${b.x},${b.y}`;
  const express = repoMap?.deps?.includes("express") ? "Express · " : "";
  return (
    <div className="arch2">
      <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
        {svc.filter((w) => w.public).map((w) => <path key={`u-${w.name}`} d={curve(user, w)} className="ae ae-in" />)}
        {pairs.map(([a, b]) => <path key={`${a}-${b}`} d={curve(at[a], at[b])} className="ae" />)}
      </svg>
      <Part x={user.x} y={user.y} title="사용자 브라우저" tone="user"
            sub={`HTTP${svc.find((w) => w.public)?.port ? ` :${svc.find((w) => w.public).port}` : ""} · API ${repoMap?.routes?.length ?? 0}개`}
            env={envOf((p, t) => (t === "aws" ? "HTTPS 공개 주소" : "이 PC 주소"))} />
      {svc.map((w) => (
        <Part key={w.name} x={w.x} y={w.y} tone="svc" title={w.kind === "http" ? "웹 서버 (백엔드)" : "백그라운드 작업"}
              sub={`${w.kind === "http" ? express : ""}${w.command ?? repoMap?.entrypoints?.start ?? ""}`} cite={cite(w.evidence)}
              env={envOf((p, t) => (t === "aws" ? "ECS Fargate" : "Docker 컨테이너"))} />
      ))}
      {store.map((st) => (
        <Part key={st.key} x={st.x} y={st.y} tone="data"
              title={st.key === "db" ? `DB · ${[st.engine, st.orm].filter(Boolean).map((n) => NAME[n] ?? n).join(" · ")}` : `파일 저장 · ${st.path}`}
              cite={cite(st.evidence)}
              note={st.key === "db" ? plan.db?.patch && "패치: SQLite → PostgreSQL" : plan.storage?.patch && "패치: 디스크·S3 겸용 저장"}
              env={envOf((p) => (st.key === "db" ? DB[p.db?.type] ?? p.db?.type : STORAGE[p.storage?.type] ?? p.storage?.type))} />
      ))}
      <div className="arch2-foot">
        {intent.secrets.length > 0 && (
          <span>비밀값 <code>{intent.secrets.join(", ")}</code> → {shown.map((t) => `${TARGET[t]} ${SECRET_STORE[t]}`).join(" · ")}</span>
        )}
        <span>로그 → {shown.map((t) => `${TARGET[t]} ${LOGS[plans[t]?.logs] ?? plans[t]?.logs}`).join(" · ")}</span>
      </div>
    </div>
  );
}

export function AppCode({ repoMap, intent, patch, plans, targets }) {
  return (
    <>
      <div className="arch2-wrap"><AppArch intent={intent} plans={plans} targets={targets} repoMap={repoMap} /></div>
      {repoMap && (
        <details className="tree-box">
          <summary>코드 트리 보기 <span className="dim">· AI가 근거로 든 파일과 패치한 파일</span></summary>
          <CodeTree repoMap={repoMap} intent={intent} patch={patch} />
        </details>
      )}
    </>
  );
}
