// 배포하려는 레포의 코드 구조. B Repo Mapper의 파일 목록(repo_map.tree)에
// C Analyzer가 근거로 든 줄(intent.evidence)과 C Code Patch가 바꾼 파일(patch)을 겹쳐 '이 파일이 무슨 역할인지'를 붙인다.

const KIND = { http: "웹 서버", worker: "백그라운드 작업" };
const STATE = { relational_db: "DB", persistent_files: "파일 저장" };

/** 파일 경로 → [{text, tone}] 역할 꼬리표. 근거 줄번호를 그대로 보여 준다(추측하지 않는다). */
function roles(intent, repoMap, patch) {
  const tags = {};
  const add = (path, text, tone) => (tags[path] ??= []).push({ text, tone });
  // 같은 역할의 근거 줄은 한 꼬리표로 모은다: "웹 서버 :3000 · 22·87줄"
  const cite = (evidence, what, tone) => {
    const lines = {};
    for (const [file, line] of evidence.map((e) => e.split(":"))) (lines[file] ??= []).push(line);
    for (const [file, ls] of Object.entries(lines)) add(file, `${what} · ${ls.join("·")}줄`, tone);
  };
  for (const w of intent?.workloads ?? []) cite(w.evidence, `${KIND[w.kind] ?? w.kind}${w.port ? ` :${w.port}` : ""}`, "role");
  for (const s of intent?.state ?? []) {
    cite(s.evidence, s.kind === "relational_db" ? `DB (${[s.engine, s.orm].filter(Boolean).join("·")})` : `${STATE[s.kind] ?? s.kind} → ${s.path}`, "state");
  }
  for (const h of repoMap?.hints ?? []) {
    if (h.type === "env" && h.name) add(h.at.split(":")[0], `환경변수 ${h.name}`, "env");
  }
  if (repoMap?.deps?.length) add("package.json", `의존성 ${repoMap.deps.length}개`, "dim");
  // C Code Patch는 모델이 아니라 검토된 고정 템플릿으로 고친다(code_patch/README.md) → 'AI'라고 쓰지 않는다.
  for (const f of patch?.files ?? []) add(f.path, f.action === "add" ? "패치로 추가" : "패치로 수정", f.action === "add" ? "added" : "changed");
  return tags;
}

/** 경로 목록 → 폴더 트리. 숨김 폴더(.github 등)는 따로 모은다. */
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

function Branch({ node, tags, depth = 0 }) {
  const dirs = Object.entries(node.dirs).sort(([a], [b]) => a.localeCompare(b));
  return (
    <ul className={depth ? "tree-sub" : "tree"}>
      {dirs.map(([name, child]) => (
        <li key={name} className="tree-dir">
          <span className="tree-name">{name}/</span>
          <Branch node={child} tags={tags} depth={depth + 1} />
        </li>
      ))}
      {node.files.sort().map((path) => (
        <li key={path} className={`tree-file${tags[path] ? " has-role" : ""}`}>
          <span className="tree-name">{path.split("/").at(-1)}</span>
          {(tags[path] ?? []).map((t) => <span key={t.text} className={`ttag tt-${t.tone}`}>{t.text}</span>)}
        </li>
      ))}
    </ul>
  );
}

export function CodeTree({ repoMap, intent, patch }) {
  if (!repoMap) return null;
  const changed = Object.values(patch ?? {})[0];
  const tags = roles(intent, repoMap, changed);
  const paths = [...new Set([...repoMap.tree, ...(changed?.files ?? []).map((f) => f.path)])];
  const hidden = paths.filter((p) => p.split("/").some((part) => part.startsWith(".")));
  const shown = paths.filter((p) => !hidden.includes(p));
  return (
    <div className="codetree">
      <Branch node={build(shown)} tags={tags} />
      {hidden.length > 0 && <p className="dim tree-hidden">설정·숨김 파일 {hidden.length}개는 생략 (.github, .env.example 등)</p>}
      {repoMap.routes?.length > 0 && (
        <div className="routes">
          <span className="dim">API</span>
          {repoMap.routes.map((r) => <code key={r}>{r}</code>)}
        </div>
      )}
    </div>
  );
}
