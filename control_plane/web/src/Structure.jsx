import { useEffect, useId, useState } from "react";

const DB = { postgres_container: "PostgreSQL 컨테이너", rds_postgres: "RDS PostgreSQL" };
const STORAGE = { volume: "Docker 볼륨", s3: "S3 버킷" };

// Plan에는 '앱이 무엇을 필요로 하나'만 있다(VPC·ALB·ARN 등은 배포기가 정한다, schemas/README.md).
// 그래서 연결(web --> db)은 plan.mermaid에서 가져오고, 둘레는 각 배포기가 실제로 만드는 구성으로 그린다:
//   AWS   = A의 terraform/foundation(공용) + terraform/app(앱 전용)
//   Local = E의 adapters/local (docker compose: 앱 컨테이너 + Postgres 컨테이너 + 볼륨)
function edgesOf(plan, label) {
  return plan.mermaid.split("\n").map((line) => line.trim().split(/\s*-->\s*/))
    .filter((pair) => pair.length === 2)
    .map(([a, b]) => `  ${label[a] ?? a} --> ${label[b] ?? b}`);
}

function service(s, target) {
  const where = target === "aws" ? "ECS Fargate" : "컨테이너";
  const port = s.port ? ` :${s.port}` : "";
  const size = target === "aws" && s.cpu ? `<br/>${s.cpu} CPU · ${s.mem}MB` : "";
  return `${s.name}["${where} · ${s.name}${port}${s.public ? "" : " (비공개)"}${size}"]`;
}

export function planToMermaid(plan) {
  const aws = plan.target === "aws";
  const label = {};
  for (const s of plan.services) label[s.name] = s.name;
  const lines = ["flowchart LR"];
  const entry = plan.services.find((s) => s.public);
  const inner = plan.services.map((s) => `    ${service(s, plan.target)}`);
  if (aws) {
    lines.push(`  user(("사용자")) -->|HTTPS| alb["ALB · HTTPS 인증서<br/>(공용 · 앱별 주소 규칙)"]`);
    lines.push(`  subgraph vpc["VPC · 비공개 서브넷"]`, ...inner, "  end");
    if (entry) lines.push(`  alb --> ${entry.name}`);
    lines.push(`  ecr[("ECR · ${plan.image_tag.slice(0, 11)}")] -. 이미지 .-> ${plan.services.map((s) => s.name).join(" & ")}`);
    if (plan.secrets?.length) lines.push(`  sec[["Secrets Manager<br/>${plan.secrets.join(", ")}"]] -. 시작 때 주입 .-> ${plan.services.map((s) => s.name).join(" & ")}`);
    if (plan.logs) lines.push(`  ${plan.services.map((s) => s.name).join(" & ")} -. 로그 .-> logs["CloudWatch Logs"]`);
    lines.push("  classDef shared stroke-dasharray: 5 4", "  class alb,ecr shared");
  } else {
    if (entry) lines.push(`  user(("사용자")) -->|"127.0.0.1"| ${entry.name}`);
    lines.push(`  subgraph net["docker compose 네트워크"]`, ...inner, "  end");
    if (plan.secrets?.length) lines.push(`  env[["app.env<br/>${plan.secrets.join(", ")}"]] -. 시작 때 주입 .-> ${plan.services.map((s) => s.name).join(" & ")}`);
  }
  const dbName = aws ? "RDS PostgreSQL<br/>(공용 서버 · 앱 전용 DB)" : DB[plan.db?.type] ?? "DB";
  if (plan.db) label.db = `db[("${dbName}")]`;
  if (plan.storage) label.storage = `storage[("${STORAGE[plan.storage.type] ?? plan.storage.type} · ${plan.storage.path}")]`;
  lines.push(...edgesOf(plan, label));
  if (aws && plan.db) lines.push("  class db shared");
  return lines.join("\n");
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
