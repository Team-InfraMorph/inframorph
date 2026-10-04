"""Deterministic repo_map rules for Node/Express/Prisma sources. No AI calls.

Routes and hints are navigation aids for the Analyzer, never confirmed Intent.
"""
import json
import re

from schemas import RepoMap

from .errors import MapperError


ROUTE = re.compile(r"\b(?:app|router)\s*\.\s*(get|post|put|patch|delete)\s*\(\s*([\"'`])(/[^\"'`\s]*)\2")
FILE_WRITE = re.compile(r"\b(?:writeFile|writeFileSync|appendFile|appendFileSync|createWriteStream)\s*\(")
PRISMA_ENV = re.compile(r"\benv\(\s*\"([A-Z][A-Z0-9_]*)\"\s*\)")
# Required configuration only: references with a `||` / `??` default are optional.
PROCESS_ENV = re.compile(r"\bprocess\.env(?:\.([A-Z][A-Z0-9_]*+)|\[\s*[\"']([A-Z][A-Z0-9_]*+)[\"']\s*\])(?!\s*(?:\|\||\?\?))")
PROVIDER = re.compile(r'^\s*provider\s*=\s*"([a-z][a-z0-9_+-]*)"', re.M,)
DATASOURCE = re.compile(r"^\s*datasource\s+\w+\s*\{([^}]*)\}", re.M)
SOURCE_SUFFIXES = (".js", ".cjs", ".mjs", ".ts")
HINT_ORDER = {"file_write": 0, "env": 1, "process": 2}


def _package(files):
    try:
        package = json.loads(files["package.json"].decode("utf-8"))
    except (ValueError, UnicodeError):
        raise MapperError("invalid_package_json") from None
    if not isinstance(package, dict):
        raise MapperError("invalid_package_json")
    scripts = package.get("scripts", {})
    deps = set()
    for key in ("dependencies", "devDependencies"):
        section = package.get(key, {})
        if not isinstance(section, dict):
            raise MapperError("invalid_package_json")
        deps.update(section)
    if not isinstance(scripts, dict) or any(not isinstance(v, str) for v in scripts.values()):
        raise MapperError("invalid_package_json")
    return scripts, sorted(deps)


def _line(text, offset):
    return text.count("\n", 0, offset) + 1


def _db(files):
    schema = files.get("prisma/schema.prisma")
    if schema is None:
        return None
    text = schema.decode("utf-8")
    blocks = list(DATASOURCE.finditer(text))
    if len(blocks) != 1:
        return {"orm": "prisma", "provider": None}
    providers = list(PROVIDER.finditer(blocks[0][1]))
    value = providers[0][1] if len(providers) == 1 else None
    return {
        "orm": "prisma",
        "provider": value
    }

def _sources(files):
    return [(path, data.decode("utf-8")) for path, data in sorted(files.items())
            if path.startswith("src/") and path.endswith(SOURCE_SUFFIXES)]


def _routes(sources):
    routes = []
    for _, text in sources:
        for match in ROUTE.finditer(text):
            route = f"{match[1].upper()} {match[3]}"
            if route not in routes:
                routes.append(route)
    return routes


def _hints(files, sources, scripts):
    hints = set()
    for path, text in sources:
        for number, line in enumerate(text.splitlines(), 1):
            if FILE_WRITE.search(line):
                hints.add(("file_write", path, number, None))
            for match in PROCESS_ENV.finditer(line):
                hints.add(("env", path, number, match[1] or match[2]))
    schema = files.get("prisma/schema.prisma")
    if schema is not None:
        for number, line in enumerate(schema.decode("utf-8").splitlines(), 1):
            for match in PRISMA_ENV.finditer(line):
                hints.add(("env", "prisma/schema.prisma", number, match[1]))
    # Extra long-running scripts suggest a separate process such as a worker.
    package = files["package.json"].decode("utf-8")
    for name, command in scripts.items():
        if name in {"start", "test"} or not command.startswith("node "):
            continue
        match = re.search(r"^\s*" + re.escape(json.dumps(name)) + r"\s*:", package, re.M)
        if match:
            hints.add(("process", "package.json", _line(package, match.start()), None))
    ordered = sorted(hints, key=lambda h: (HINT_ORDER[h[0]], h[1], h[2], h[3] or ""))
    return [{"type": kind, "at": f"{path}:{number}", "name": name} for kind, path, number, name in ordered]


def build_repo_map(commit: str, files: dict[str, bytes]) -> RepoMap:
    scripts, deps = _package(files)
    sources = _sources(files)
    return RepoMap.model_validate({
        "commit": commit,
        "tree": sorted(files),
        "deps": deps,
        "db": _db(files),
        "entrypoints": {name: command for name, command in scripts.items() if name != "test"},
        "routes": _routes(sources),
        "hints": _hints(files, sources, scripts),
    })
