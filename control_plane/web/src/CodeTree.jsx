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

/** 행 하나 = 앱이 필요로 하는 것 하나. 열 = 대상별로 그게 무엇이 되는지. */
function rowsOf(intent, plans, targets) {
  const svc = (t, name) => plans[t]?.services.find((s) => s.name === name);
  const rows = [];
  for (const w of intent.workloads) {
    rows.push({
      need: w.kind === "http" ? "웹 서버" : "백그라운드 작업",
      detail: [w.port && `:${w.port}`, svc(targets[0], w.name)?.health, w.public ? "외부 공개" : "비공개"].filter(Boolean).join(" · "),
      evidence: cite(w.evidence),
      to: Object.fromEntries(targets.map((t) => [t, t === "aws"
        ? `ECS Fargate${w.public ? " · 로드밸런서(HTTPS) 뒤" : ""}` : "Docker 컨테이너"])),
    });
  }
  for (const s of intent.state) {
    const db = s.kind === "relational_db";
    rows.push({
      need: db ? "DB" : "파일 저장",
      detail: db ? [s.engine, s.orm].filter(Boolean).join(" · ") : s.path,
      evidence: cite(s.evidence),
      to: Object.fromEntries(targets.map((t) => [t, db ? DB[plans[t]?.db?.type] ?? plans[t]?.db?.type ?? "—"
        : STORAGE[plans[t]?.storage?.type] ?? plans[t]?.storage?.type ?? "—"])),
      fix: db ? plans[targets[0]]?.db?.patch && "SQLite → PostgreSQL로 코드 수정" : plans[targets[0]]?.storage?.patch && "저장 코드를 디스크·S3 겸용으로 교체",
    });
  }
  if (intent.secrets.length) {
    rows.push({ need: "비밀값", detail: intent.secrets.join(", "), to: Object.fromEntries(targets.map((t) => [t, SECRET_STORE[t]])) });
  }
  rows.push({ need: "로그", to: Object.fromEntries(targets.map((t) => [t, LOGS[plans[t]?.logs] ?? plans[t]?.logs ?? "—"])) });
  return rows;
}

function Translation({ intent, plans, targets }) {
  const shown = targets.filter((t) => plans[t]);
  if (!intent || !shown.length) return null;
  return (
    <table className="xlate">
      <thead>
        <tr><th>앱이 필요로 하는 것 <span className="dim">· AI가 찾은 근거</span></th>{shown.map((t) => <th key={t}>{TARGET[t]}</th>)}</tr>
      </thead>
      <tbody>
        {rowsOf(intent, plans, shown).map((r) => (
          <tr key={r.need + (r.detail ?? "")}>
            <td>
              <div className="need"><strong>{r.need}</strong>{r.detail && <span className="dim"> {r.detail}</span>}</div>
              {r.evidence && <div className="cite">{r.evidence}</div>}
              {r.fix && <div className="fix">{r.fix}</div>}
            </td>
            {shown.map((t) => <td key={t} className={`to to-${t}`}>{r.to[t]}</td>)}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function AppCode({ repoMap, intent, patch, plans, targets }) {
  return (
    <div className="appcode">
      {repoMap && <CodeTree repoMap={repoMap} intent={intent} patch={patch} />}
      <Translation intent={intent} plans={plans} targets={targets} />
    </div>
  );
}
