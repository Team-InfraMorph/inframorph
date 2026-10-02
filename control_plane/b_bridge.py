"""Explicit B command bridge. No default implementation or implicit fixture mode."""
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess

from schemas import Plan, RepoMap
from analyzer.local_verify import child_environment


ROOT = Path(__file__).resolve().parents[1]
LIMIT = 120_000


@dataclass(frozen=True)
class MappedSource:
    snapshot: Path
    repo_map: RepoMap


def call_json(command, payload):
    if not isinstance(command, (list, tuple)) or not command or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in command):
        raise ValueError("invalid_b_command")
    # Commands belong to the operator. Repo values are stdin data, never shell text.
    with __import__("tempfile").TemporaryFile() as output:
        result = subprocess.run(command, input=json.dumps(payload).encode(), stdout=output,
                                stderr=subprocess.DEVNULL, cwd=ROOT, env=child_environment(), timeout=90)
        output.seek(0)
        raw = output.read(LIMIT + 1)
    if result.returncode or len(raw) > LIMIT:
        raise ValueError("b_command_failed")
    return json.loads(raw)


class BCommands:
    def __init__(self, *, mapper_command, planner_command):
        if not mapper_command or not planner_command:
            raise ValueError("b_modules_unavailable")
        self.mapper_command = mapper_command
        self.planner_command = planner_command

    def map(self, project, deployment, output):
        result = call_json(self.mapper_command, {"repo_url": project["repo_url"], "branch": project["branch"],
            "source_revision": deployment["commit_sha"], "output_dir": str(output)})
        if not isinstance(result, dict) or set(result) != {"snapshot", "repo_map"}:
            raise ValueError("invalid_b_mapping")
        name = result["snapshot"]
        if not isinstance(name, str) or name != "snapshot":
            raise ValueError("invalid_b_snapshot")
        source = Path(output) / name
        if not source.is_dir() or any(p.is_symlink() for p in (source, *source.parents)):
            raise ValueError("invalid_b_snapshot")
        mapping = RepoMap.model_validate(result["repo_map"])
        if deployment["commit_sha"] is not None and mapping.commit != deployment["commit_sha"]:
            raise ValueError("b_revision_mismatch")
        return MappedSource(source, mapping)

    def plan(self, intent):
        return Plan.model_validate(call_json(self.planner_command,
                                            {"intent": intent.model_dump(mode="json"), "target": "local"}))


class DemoModules:
    """Opt-in development fixtures, not the missing B implementation."""
    def map(self, project, deployment, output):
        if project["repo_url"].rstrip("/").removesuffix(".git") != "https://github.com/Team-InfraMorph/demo-app":
            raise ValueError("demo_repository_required")
        for case in ("v1", "v2"):
            fixture = ROOT / "tests/fixtures/analyzer" / case
            mapping = RepoMap.model_validate_json((fixture / "repo_map.json").read_text())
            if deployment["commit_sha"] not in (None, mapping.commit):
                continue
            source = Path(output) / "snapshot"
            shutil.copytree(fixture / "snapshot", source)
            return MappedSource(source, mapping)
        raise ValueError("unsupported_demo_revision")

    def plan(self, intent):
        # This is a fixture, not an attempt to implement B's mapping rules.
        case = "v2" if any(w.kind.value == "worker" for w in intent.workloads) else "v1"
        return Plan.model_validate_json((ROOT / f"schemas/fixtures/{case}/plan.local.json").read_text())

    def replay(self, mapping):
        case = "v2" if "src/worker.js" in mapping.tree else "v1"
        return ROOT / f"tests/fixtures/analyzer/{case}/replay.json"
