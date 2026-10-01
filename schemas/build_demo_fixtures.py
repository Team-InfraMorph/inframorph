import json
import subprocess
from pathlib import Path

from . import Intent, Plan, RepoMap


ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT.parent / "demo-app"
OUTPUT = ROOT / "schemas" / "fixtures"


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(DEMO), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.rstrip("\n")


def evidence(sha: str, path: str, needle: str) -> str:
    lines = git("show", f"{sha}:{path}").splitlines()
    matches = [
        number
        for number, line in enumerate(lines, 1)
        if needle in line
    ]
    if len(matches) != 1:
        raise ValueError(f"{path}: {needle!r} 검색 결과가 {matches}입니다")
    return f"{path}:{matches[0]}"


def make(label: str, tag: str) -> None:
    sha = git("rev-parse", "--verify", f"{tag}^{{commit}}")
    package = json.loads(git("show", f"{sha}:package.json"))
    scripts = package["scripts"]

    health_at = evidence(sha, "src/server.js", 'app.get("/health"')
    listen_at = evidence(sha, "src/server.js", "app.listen(")
    db_at = evidence(sha, "prisma/schema.prisma", 'provider = "sqlite"')
    env_at = evidence(sha, "prisma/schema.prisma", 'env("DATABASE_URL")')
    write_at = evidence(sha, "src/images.js", "fs.writeFile(")
    read_at = evidence(sha, "src/images.js", "fs.readFile(")

    hints = [
        {"type": "file_write", "at": write_at},
        {"type": "env", "name": "DATABASE_URL", "at": env_at},
    ]

    workloads = [{
        "name": "web",
        "kind": "http",
        "port": 3000,
        "public": True,
        "health": "/health",
        "evidence": [health_at, listen_at],
    }]

    services = [{
        "name": "web",
        "kind": "http",
        "cpu": 256,
        "mem": 512,
        "port": 3000,
        "health": "/health",
        "public": True,
    }]

    if label == "v2":
        worker_at = evidence(sha, "src/worker.js", "setInterval(")
        script_at = evidence(sha, "package.json", '"worker":')
        hints.append({"type": "process", "at": script_at})
        workloads.append({
            "name": "worker",
            "kind": "worker",
            "public": False,
            "command": scripts["worker"],
            "evidence": [worker_at, script_at],
        })
        services.append({
            "name": "worker",
            "kind": "worker",
            "cpu": 256,
            "mem": 512,
            "public": False,
            "command": scripts["worker"],
        })

    dependencies = {
        **package.get("dependencies", {}),
        **package.get("devDependencies", {}),
    }
    entrypoints = {"start": scripts["start"]}
    if "worker" in scripts:
        entrypoints["worker"] = scripts["worker"]

    repo_map = {
        "commit": sha,
        "tree": git("ls-tree", "-r", "--name-only", sha).splitlines(),
        "deps": sorted(dependencies),
        "db": {"orm": "prisma", "provider": "sqlite"},
        "entrypoints": entrypoints,
        "routes": [
            "GET /",
            "GET /health",
            "POST /api/notes",
            "GET /api/notes",
            "POST /api/images",
            "GET /api/images/:key",
        ],
        "hints": hints,
    }

    intent = {
        "source_revision": sha,
        "app": "demo-app",
        "runtime": "node22",
        "workloads": workloads,
        "state": [
            {
                "kind": "relational_db",
                "engine": "sqlite",
                "orm": "prisma",
                "evidence": [db_at],
            },
            {
                "kind": "persistent_files",
                "path": "uploads/",
                "reason": "업로드한 사진이 재시작 후에도 남아야 함",
                "evidence": [write_at, read_at],
            },
        ],
        "secrets": ["DATABASE_URL"],
        "config": {},
        "unknowns": [],
    }

    diagram = "flowchart LR\nweb --> db\nweb --> storage"
    if label == "v2":
        diagram += "\nworker --> db"

    def plan(target: str) -> dict:
        is_aws = target == "aws"
        return {
            "source_revision": sha,
            "target": target,
            "app": "demo-app",
            "image_tag": f"app:{sha}",
            "services": services,
            "db": {
                "type": "rds_postgres" if is_aws else "postgres_container",
                "patch": "sqlite_to_postgres",
            },
            "storage": {
                "type": "s3" if is_aws else "volume",
                "patch": "fs_to_storage",
                "path": "uploads/",
            },
            "secrets": ["DATABASE_URL"],
            "config": {"STORAGE_DRIVER": "s3" if is_aws else "fs"},
            "logs": "cloudwatch" if is_aws else "docker",
            "est_monthly_krw": None if is_aws else 0,
            "mermaid": diagram,
        }

    output_dir = OUTPUT / label
    output_dir.mkdir(parents=True, exist_ok=True)

    files = {
        "repo_map.json": (RepoMap, repo_map),
        "intent.json": (Intent, intent),
        "plan.local.json": (Plan, plan("local")),
        "plan.aws.json": (Plan, plan("aws")),
    }
    for filename, (model, data) in files.items():
        checked = model.model_validate(data)
        path = output_dir / filename
        path.write_text(
            checked.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        print(path)


if __name__ == "__main__":
    if not DEMO.is_dir():
        raise SystemExit(f"demo-app이 없습니다: {DEMO}")
    make("v1", "demo-v1")
    make("v2", "demo-v2")