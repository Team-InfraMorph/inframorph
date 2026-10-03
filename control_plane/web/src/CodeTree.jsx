import { useState } from "react";

// 배포할 앱 한 장: 왼쪽 = 코드 트리(B repo_map.tree), 오른쪽 = '앱이 필요로 하는 것 → 대상별로 무엇이 되나'.
// 필요(근거 줄)는 C Analyzer의 intent, 대상별 구성은 B Planner의 plan, 바뀐 파일은 C Code Patch 결과에서 온다.

const DB = { postgres_container: "PostgreSQL 컨테이너", rds_postgres: "RDS PostgreSQL" };
const STORAGE = { volume: "Docker 볼륨", s3: "S3 버킷" };
const LOGS = { docker: "Docker 로그", cloudwatch: "CloudWatch" };

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

function Branch({ node, role, change, hl, onHover, depth = 0 }) {
  return (
    <ul className={depth ? "tree-sub" : "tree"}>
      {Object.entries(node.dirs).sort(([a], [b]) => a.localeCompare(b)).map(([name, child]) => (
        <li key={name}><span className="tree-dir">{name}/</span><Branch node={child} role={role} change={change} hl={hl} onHover={onHover} depth={depth + 1} /></li>
      ))}
      {node.files.sort().map((path) => (
        <li key={path} className={`tree-file${role[path] ? " has-role" : ""}${hl.has(path) ? " hl" : ""}`}
            onMouseEnter={() => onHover(path)} onMouseLeave={() => onHover(null)}>
          <span className="tree-name">{path.split("/").at(-1)}</span>
          {role[path] && <span className="tree-role">{role[path]}</span>}
          {change[path] && <span className={`tree-git g-${change[path]}`} title={change[path] === "A" ? "패치로 추가" : "패치로 수정"}>{change[path]}</span>}
        </li>
      ))}
    </ul>
  );
}

function CodeTree({ repoMap, intent, patch, hl = new Set(), onHover = () => {} }) {
  const changed = Object.values(patch ?? {})[0];
  const change = Object.fromEntries((changed?.files ?? []).map((f) => [f.path, f.action === "add" ? "A" : "M"]));
  const paths = [...new Set([...repoMap.tree, ...Object.keys(change)])];
  const hidden = paths.filter((p) => p.split("/").some((part) => part.startsWith(".")));
  return (
    <div className="codetree">
      <Branch node={build(paths.filter((p) => !hidden.includes(p)))} role={roleOf(intent)} change={change} hl={hl} onHover={onHover} />
      <p className="tree-legend">
        <span className="tree-git g-A">A</span> 패치로 추가 <span className="tree-git g-M">M</span> 패치로 수정
        {hidden.length > 0 && <> · 숨김·설정 파일 {hidden.length}개 생략</>}
      </p>
    </div>
  );
}

const NAME = { sqlite: "SQLite", postgres: "PostgreSQL", postgresql: "PostgreSQL", mysql: "MySQL", prisma: "Prisma" };

/** 클라우드 부품과 그 부품을 만든 코드 파일. 파일은 AI 근거(intent.evidence)와 패치한 파일에서 온다. */
function partsOf(intent, plan, patchFiles) {
  const files = (ev) => [...new Set(ev.map((e) => e.split(":")[0]))];
  const added = (re) => patchFiles.filter((f) => re.test(f.path)).map((f) => f.path);
  const svc = intent.workloads.map((w) => ({ id: w.name, w, files: files(w.evidence) }));
  const data = intent.state.map((st) => {
    const db = st.kind === "relational_db";
    return {
      id: db ? "db" : "storage", st, db,
      files: [...new Set([...files(st.evidence), ...added(db ? /prisma|schema|db/i : /storage|upload|image/i)])],
      fix: db ? plan.db?.patch && "패치: SQLite → PostgreSQL" : plan.storage?.patch && "패치: 디스크·S3 겸용 저장",
    };
  });
  return { svc, data };
}

function Box({ part, title, role, sub, fix, shared, tone, hl, onHover }) {
  const on = part && part.files.some((f) => hl.has(f));
  return (
    <div className={`cbox${tone ? ` cb-${tone}` : ""}${on ? " on" : ""}`}
         onMouseEnter={part ? () => onHover(part.files) : undefined} onMouseLeave={part ? () => onHover(null) : undefined}>
      <div className="cb-title">{title}{shared && <span className="chip">공용</span>}</div>
      {role && <div className="cb-role">{role}</div>}
      {sub && <div className="cb-sub">{sub}</div>}
      {fix && <div className="cb-fix">{fix}</div>}
      {part?.files.length > 0 && <div className="cb-files">{part.files.map((f) => <code key={f}>{f.split("/").at(-1)}</code>)}</div>}
    </div>
  );
}

const Down = ({ label }) => <div className="cdown"><span>{label}</span></div>;

/** 이 레포에 맞게 실제로 배포된 구조. AWS = A의 terraform(foundation 공용 + app 전용), Local = E의 docker compose. */
const USER = { aws: ["사용자 (인터넷 어디서나)", "HTTPS 공개 주소"], onprem: ["사용자 (사내망 · Tailscale)", "사내 서버 주소"], local: ["사용자 (이 PC)", "127.0.0.1"] };
const ZONE = { aws: "VPC 비공개 구역 · 직접 접속 불가", onprem: "사내 서버 Docker · 같은 이미지", local: "docker compose · 테스트용" };
const TAB = { local: "Local 테스트 · 내 노트북", onprem: "온프레미스 · 사내 서버", aws: "AWS · 서울 리전" };

function CloudArch({ target, intent, plan, parts, url, hl, onHover, title }) {
  const aws = target === "aws";
  const web = parts.svc.find((p) => p.w.public);
  const host = url?.replace(/^https?:\/\//, "").replace(/\/.*$/, "");
  const svcTitle = (p) => (aws ? "ECS Fargate" : "앱 컨테이너");
  return (
    <div className={`carch ca-${target}`}>
      {title && <div className={`ca-title ct-${target}`}>{title}</div>}
      <Box title={USER[target][0]} sub={host ?? USER[target][1]} tone="user" hl={hl} onHover={onHover} />
      <Down label={aws ? "HTTPS" : `HTTP${web?.w.port ? ` :${web.w.port}` : ""}`} />
      {aws && <>
        <Box title="로드밸런서 (ALB)" sub="HTTPS 인증서 · 앱 주소별로 나눠 보냄" shared tone="edge" hl={hl} onHover={onHover} />
        <Down />
      </>}
      <div className="czone">
        <div className="cz-label">{ZONE[target]}</div>
        <div className="crow">
          {parts.svc.map((p) => (
            <Box key={p.id} part={p} title={svcTitle(p)} tone="svc" hl={hl} onHover={onHover}
                 role={`${p.w.kind === "http" ? "웹 서버" : "백그라운드 작업"}${p.w.port ? ` :${p.w.port}` : ""}`}
                 sub={aws ? `${plan.services.find((s) => s.name === p.id)?.cpu ?? ""} CPU · ${plan.services.find((s) => s.name === p.id)?.mem ?? ""}MB` : p.w.command ?? ""} />
          ))}
        </div>
      </div>
      <Down />
      <div className="crow">
        {parts.data.map((p) => (
          <Box key={p.id} part={p} tone="data" hl={hl} onHover={onHover} fix={p.fix}
               title={p.db ? DB[plan.db?.type] ?? plan.db?.type : STORAGE[plan.storage?.type] ?? plan.storage?.type}
               role={p.db ? "DB" : `업로드 파일 · ${p.st.path}`}
               sub={p.db ? (aws ? "공용 DB 서버 안의 앱 전용 DB" : "데이터는 Docker 볼륨에 보관") : aws ? "재배포해도 파일 유지" : "재시작해도 파일 유지"}
               shared={aws && p.db} />
        ))}
      </div>
      <div className="cside">
        {aws && <span><b>ECR</b> 앱 이미지 {plan.image_tag.slice(0, 11)}</span>}
        {intent.secrets.length > 0 && <span><b>{aws ? "Secrets Manager" : "app.env"}</b> {intent.secrets.join(", ")}</span>}
        <span><b>{LOGS[plan.logs] ?? plan.logs}</b> 로그</span>
      </div>
    </div>
  );
}

/** 왼쪽 = 배포할 레포의 코드 트리, 오른쪽 = 그 레포에 맞게 배포된 구조. 파일과 부품에 마우스를 올리면 서로 강조된다. */
export function AppCode({ repoMap, intent, patch, plans, targets, urls = {} }) {
  const shown = targets.filter((t) => plans[t]);
  // 배포 대상(온프레미스·AWS)이 둘 이상이면 나란히 보여 준다. Local 테스트는 따로 고를 수 있다.
  const deploys = shown.filter((t) => t !== "local");
  const views = deploys.length > 1 ? ["deploy", ...shown.filter((t) => t === "local")] : shown;
  const [tab, setTab] = useState(null); // 고르기 전에는 배포 대상(나란히) 또는 AWS를 보여 준다. 설계도가 늦게 와도 따라간다
  const [hl, setHl] = useState(new Set());
  const current = tab && views.includes(tab) ? tab : ["deploy", "aws", "onprem", "local"].find((t) => views.includes(t));
  const patchFiles = Object.values(patch ?? {})[0]?.files ?? [];
  if (!intent || !current) return null;
  const panels = current === "deploy" ? deploys : [current];
  const parts = partsOf(intent, plans[panels[0]], patchFiles);
  const hover = (x) => setHl(new Set(x == null ? [] : Array.isArray(x) ? x : [x]));
  // 트리의 파일에 올리면 → 그 파일을 쓰는 부품의 파일 전부를 강조(부품이 켜진다)
  const fromTree = (path) => hover(path ? [...parts.svc, ...parts.data].filter((p) => p.files.includes(path)).flatMap((p) => p.files).concat(path) : null);
  return (
    <div className="appcode2">
      <div className="ac-left">
        <div className="ac-head">레포 코드 <span className="dim">· 이름 옆 = AI가 찾은 역할</span></div>
        {repoMap && <CodeTree repoMap={repoMap} intent={intent} patch={patch} hl={hl} onHover={fromTree} />}
      </div>
      <div className="ac-right">
        <div className="ac-head ac-tabs">
          {views.map((t) => (
            <button key={t} className={`tab tab-${t}${t === current ? " on" : ""}`} onClick={() => setTab(t)}>
              {t === "deploy" ? `배포 대상 · ${deploys.map((d) => TAB[d].split(" · ")[0]).join(" + ")} 동시` : TAB[t] ?? t}
            </button>
          ))}
          <span className="dim">이 레포에 맞게 배포된 구조</span>
        </div>
        <div className={panels.length > 1 ? "carch-pair" : undefined}>
          {panels.map((t) => (
            <CloudArch key={t} target={t} intent={intent} plan={plans[t]} parts={partsOf(intent, plans[t], patchFiles)}
                       url={urls[t]} hl={hl} onHover={hover} title={panels.length > 1 ? TAB[t] : null} />
          ))}
        </div>
      </div>
    </div>
  );
}
