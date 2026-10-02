"""Explicit B command bridge. No default implementation or implicit fixture mode."""
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import time

from schemas import Plan, RepoMap
from analyzer.local_verify import child_environment


ROOT = Path(__file__).resolve().parents[1]
LIMIT = 120_000
TIMEOUT = 90


class BCommandError(ValueError):
    """Fixed public diagnostic; never include command output or source data."""


def checked_command(command):
    if not isinstance(command, (list, tuple)) or not command or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in command):
        raise BCommandError("invalid_b_command")
    return tuple(command)


def unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate_json_key")
        result[name] = value
    return result


def invalid_constant(value):
    raise ValueError("non_finite_json")


@dataclass(frozen=True)
class MappedSource:
    snapshot: Path
    repo_map: RepoMap


def call_json(command, payload, *, timeout=TIMEOUT):
    command = checked_command(command)
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= TIMEOUT:
        raise BCommandError("invalid_b_timeout")
    encoded = json.dumps(payload, allow_nan=False).encode()
    if len(encoded) > LIMIT:
        raise BCommandError("b_request_limit")
    # Commands belong to the operator. Repo values are stdin data, never shell text.
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd=ROOT, env=child_environment(), start_new_session=True)
    except OSError:
        raise BCommandError("b_command_unavailable") from None
    deadline, written, raw = time.monotonic() + timeout, 0, bytearray()
    try:
        with selectors.DefaultSelector() as selected:
            for pipe, event in ((process.stdin, selectors.EVENT_WRITE), (process.stdout, selectors.EVENT_READ)):
                os.set_blocking(pipe.fileno(), False)
                selected.register(pipe, event)
            while selected.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BCommandError("b_command_timeout")
                for key, _ in selected.select(remaining):
                    if key.fileobj is process.stdin:
                        try:
                            written += os.write(process.stdin.fileno(), encoded[written:written + 16_384])
                        except BrokenPipeError:
                            written = len(encoded)
                        if written == len(encoded):
                            selected.unregister(process.stdin)
                            process.stdin.close()
                    else:
                        chunk = os.read(process.stdout.fileno(), 16_384)
                        if not chunk:
                            selected.unregister(process.stdout)
                        elif len(raw) + len(chunk) > LIMIT:
                            raise BCommandError("b_reply_limit")
                        else:
                            raw.extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BCommandError("b_command_timeout")
        try:
            code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise BCommandError("b_command_timeout") from None
        if code:
            raise BCommandError("b_command_failed")
        try:
            return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=invalid_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise BCommandError("invalid_b_json") from None
    finally:
        # A child may exit while its descendants retain stdout or keep running.
        # Kill only this command's new process group, including on success.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        process.stdin.close()
        process.stdout.close()


class BCommands:
    def __init__(self, *, mapper_command, planner_command, timeout=TIMEOUT):
        if not mapper_command or not planner_command:
            raise ValueError("b_modules_unavailable")
        self.mapper_command = list(checked_command(mapper_command))
        self.planner_command = list(checked_command(planner_command))
        self.timeout = timeout

    def map(self, project, deployment, output):
        result = call_json(self.mapper_command, {"repo_url": project["repo_url"], "branch": project["branch"],
            "source_revision": deployment["commit_sha"], "output_dir": str(output)}, timeout=self.timeout)
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
                                            {"intent": intent.model_dump(mode="json"), "target": "local"}, timeout=self.timeout))


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
