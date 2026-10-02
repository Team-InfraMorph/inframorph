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

/** 앱 → 배포 모습 변환도. 왼쪽 = 개발자가 만든 앱 그대로, 오른쪽 = 대상별로 실제 돌아가는 모습. 같은 부품은 같은 줄. */
function Morph({ intent, plans, targets, urls }) {
  const shown = targets.filter((t) => plans[t]);
  if (!intent || !shown.length) return null;
  const plan = plans[shown[0]];
  const web = intent.workloads.find((w) => w.public);
  const host = (u) => u?.replace(/^https?:\/\//, "").replace(/\/.*$/, "");
  const rows = [];
  rows.push({
    id: "entry", what: "접속 주소",
    orig: { main: `localhost${web?.port ? `:${web.port}` : ""}`, sub: "내 노트북에서만 열림" },
    to: { local: { main: "이 PC 주소", sub: urls.local ? host(urls.local) : "Docker가 연 포트" },
          aws: { main: "HTTPS 공개 주소", sub: urls.aws ? host(urls.aws) : "로드밸런서(ALB) + HTTPS 인증서" } },
  });
  for (const w of intent.workloads) {
    rows.push({
      id: w.name, what: w.kind === "http" ? "웹 서버 (백엔드)" : "백그라운드 작업",
      orig: { main: w.command ?? "node src/server.js", sub: cite(w.evidence) },
      to: { local: { main: "Docker 컨테이너", sub: "이미지 하나로 실행" },
            aws: { main: "ECS Fargate", sub: w.public ? "서버 관리 없이 컨테이너 실행" : "비공개 서비스" } },
    });
  }
  for (const st of intent.state) {
    const db = st.kind === "relational_db";
    rows.push({
      id: st.kind, what: db ? "DB" : "업로드 파일",
      orig: { main: db ? `${NAME[st.engine] ?? st.engine} 파일` : `${st.path} 폴더`, sub: cite(st.evidence) },
      fix: db ? plan.db?.patch && "SQLite → PostgreSQL" : plan.storage?.patch && "디스크·S3 겸용",
      to: Object.fromEntries(shown.map((t) => [t, db
        ? { main: DB[plans[t].db?.type] ?? plans[t].db?.type, sub: t === "aws" ? "공용 DB 서버 안의 전용 DB" : "데이터는 Docker 볼륨에 보관" }
        : { main: STORAGE[plans[t].storage?.type] ?? plans[t].storage?.type, sub: t === "aws" ? "재배포해도 파일 유지" : "재시작해도 파일 유지" }])),
    });
  }
  if (intent.secrets.length) {
    rows.push({
      id: "secret", what: "비밀값",
      orig: { main: intent.secrets.join(", "), sub: ".env 파일" },
      to: { local: { main: "app.env 파일", sub: "배포 때 생성" }, aws: { main: "Secrets Manager", sub: "앱 시작 때만 꺼내 줌" } },
    });
  }
  const cell = (c) => (c ? <><div className="m-main">{c.main}</div>{c.sub && <div className="m-sub">{c.sub}</div>}</> : null);
  const last = rows.length - 1;
  return (
    <div className="morph" style={{ "--cols": shown.length }}>
      <div className="m-h m-col0"><span className="m-tag">원래 앱</span>개발자가 만든 그대로</div>
      <div className="m-h m-arrow-h">InfraMorph<br />자동 변환</div>
      {shown.map((t) => <div key={t} className={`m-h m-col m-${t}`}><span className="m-tag">{TARGET[t]}</span>{t === "aws" ? "AWS 서울 리전" : "내 노트북 Docker"}</div>)}
      {rows.map((r, i) => (
        <div key={r.id} className={`m-row${i === last ? " m-last" : ""}`}>
          <div className="m-c m-col0"><div className="m-what">{r.what}</div>{cell(r.orig)}</div>
          <div className="m-c m-arrow">{r.fix ? <span className="m-fix">{r.fix}</span> : null}</div>
          {shown.map((t) => <div key={t} className={`m-c m-col m-${t}`}>{cell(r.to[t])}</div>)}
        </div>
      ))}
    </div>
  );
}

export function AppCode({ repoMap, intent, patch, plans, targets, urls = {} }) {
  return (
    <>
      <div className="morph-wrap"><Morph intent={intent} plans={plans} targets={targets} urls={urls} /></div>
      {repoMap && (
        <details className="tree-box">
          <summary>코드 트리 보기 <span className="dim">· AI가 근거로 든 파일과 패치한 파일</span></summary>
          <CodeTree repoMap={repoMap} intent={intent} patch={patch} />
        </details>
      )}
    </>
  );
}
