import { useEffect, useId, useState } from "react";

const DB = { postgres_container: "PostgreSQL 컨테이너", rds_postgres: "RDS PostgreSQL" };
const STORAGE = { volume: "Docker 볼륨", s3: "S3" };

// plan.mermaid의 연결(web --> db)은 그대로 두고, 노드 이름을 대상별 실제 구성으로 바꾼다.
export function planToMermaid(plan) {
  const label = { db: `db[("${DB[plan.db?.type] ?? "DB"}")]` };
  if (plan.storage) label.storage = `storage[("${STORAGE[plan.storage.type]} · ${plan.storage.path}")]`;
  for (const s of plan.services) {
    label[s.name] = `${s.name}["${s.name} · ${s.kind}${s.port ? ` :${s.port}` : ""}"]`;
  }
  const edges = plan.mermaid
    .split("\n")
    .map((line) => line.trim().split(/\s*-->\s*/))
    .filter((pair) => pair.length === 2)
    .map(([a, b]) => `  ${label[a] ?? a} --> ${label[b] ?? b}`);
  const entry = plan.services.find((s) => s.public);
  if (entry) edges.unshift(`  user(("사용자")) --> ${label[entry.name]}`);
  return ["flowchart LR", ...edges].join("\n");
}

let mermaidReady;
function loadMermaid() {
  // 구조도가 처음 필요할 때만 불러와 첫 화면을 가볍게 한다.
  mermaidReady ??= import("mermaid").then(({ default: mermaid }) => {
    const dark = window.matchMedia?.("(prefers-color-scheme: dark)").matches;
    mermaid.initialize({ startOnLoad: false, theme: dark ? "dark" : "default", securityLevel: "strict" });
    return mermaid;
  });
  return mermaidReady;
}

export function Structure({ plan }) {
  const id = `m${useId().replace(/[^a-zA-Z0-9]/g, "")}`;
  const [svg, setSvg] = useState("");
  const text = plan ? planToMermaid(plan) : "";

  useEffect(() => {
    let alive = true;
    if (!text) return undefined;
    loadMermaid()
      .then((mermaid) => mermaid.render(id, text))
      .then((out) => alive && setSvg(out.svg))
      .catch(() => alive && setSvg(""));
    return () => { alive = false; };
  }, [id, text]);

  return svg ? <div className="structure" dangerouslySetInnerHTML={{ __html: svg }} /> : <pre className="dim">{text}</pre>;
}
