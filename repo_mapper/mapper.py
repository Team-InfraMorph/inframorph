import json
from pathlib import Path
import re

from schemas import RepoMap

def line_of(data, fragment):
    for number, line in enumerate(data.decode().splitlines(), 1):
        if fragment in line:
            return number
    raise ValueError("unsupported_demo_source")

def build_map(revision, files):
    package = json.loads(files["package.json"])
    if not isinstance(package, dict):
        raise ValueError("invalid_package")

    scripts = package.get("scripts", {})
    dependencies = package.get("dependencies", {})
    dev_dependencies = package.get("devDependencies", {})
    if not all(isinstance(item, dict) for item in
               (scripts, dependencies, dev_dependencies)):
        raise ValueError("invalid_package")

    has_worker = "src/worker.js" in files
    if (package.get("name") != "demo-app" or
            scripts.get("start") != "node src/server.js" or
            has_worker != (scripts.get("worker") == "node src/worker.js")):
        raise ValueError("unsupported_demo_package")

    deps = sorted(set(dependencies) | set(dev_dependencies))
    schema = files["prisma/schema.prisma"].decode()
    provider = re.search(r'provider\s*=\s*"(sqlite|postgresql)"', schema)
    if provider is None:
        raise ValueError("unsupported_database")

    server = files["src/server.js"].decode()
    routes = [
        f"{method.upper()} {path}"
        for method, path in re.findall(
            r'app\.(get|post|put|patch|delete)\s*\(\s*["\']([^"\']+)["\']',
            server,
        )
    ]

    image_line = line_of(files["src/images.js"], "fs.writeFile")
    db_line = line_of(files["prisma/schema.prisma"], 'env("DATABASE_URL")')
    hints = [
        {"type": "file_write", "at": f"src/images.js:{image_line}"},
        {
            "type": "env",
            "at": f"prisma/schema.prisma:{db_line}",
            "name": "DATABASE_URL",
        },
    ]

    entrypoints = {"start": scripts["start"]}
    if has_worker:
        entrypoints["worker"] = scripts["worker"]
        worker_line = line_of(files["package.json"], '"worker":')
        hints.append({
            "type": "process",
            "at": f"package.json:{worker_line}",
        })

    return RepoMap.model_validate({
        "commit": revision,
        "tree": sorted(files),
        "deps": deps,
        "db": {"orm": "prisma", "provider": provider.group(1)},
        "entrypoints": entrypoints,
        "routes": routes,
        "hints": hints,
    })


def map_snapshot(snapshot, output_dir):
    """Create repo_map.json from a verified snapshot and its source commit."""
    output = Path(output_dir).resolve(strict=True)
    if snapshot.path != output / "snapshot":
        raise ValueError("snapshot_output_mismatch")
    path = output / "repo_map.json"
    mapping = build_map(snapshot.commit, snapshot.files)
    created = False
    try:
        with path.open("x", encoding="utf-8") as stream:
            created = True
            stream.write(mapping.model_dump_json(indent=2) + "\n")
    except Exception:
        if created:
            path.unlink(missing_ok=True)
        raise
    return mapping
