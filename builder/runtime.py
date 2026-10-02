"""Build only the exact in-memory bytes returned by the Policy Gate."""

import contextlib
import fcntl
import json
import math
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from policy_gate.gate import (
    PolicyError,
    clean_env,
    require,
    sha,
    validate_patch,
    write_files,
)
from schemas import BuildArtifact, DeployEvent, Plan

ROOT = Path(__file__).parent


class RuntimeFailure(RuntimeError):
    pass


def failure_code(exc, fallback):
    if isinstance(exc, (PolicyError, RuntimeFailure)) and re.fullmatch(
        r"[a-z_]{1,80}", str(exc)
    ):
        return str(exc)
    return fallback


def run(args, *, timeout=120, cwd=None):
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=clean_env(),
        )
    except (OSError, subprocess.SubprocessError):
        raise RuntimeFailure("command_unavailable_or_timeout") from None
    if result.returncode:
        raise RuntimeFailure("command_failed")
    return result.stdout.strip()


def emit(sink, deployment_id, target, step, status, **kwargs):
    if sink:
        sink(
            DeployEvent(
                deployment_id=deployment_id,
                ts=datetime.now(timezone.utc),
                target=target,
                step=step,
                status=status,
                **kwargs,
            )
        )


def check_build_profile(files):
    require(
        all(
            name in files
            for name in (
                "package.json",
                "package-lock.json",
                "prisma/schema.prisma",
                "src/server.js",
            )
        ),
        "build_profile_incomplete",
    )
    try:
        package = json.loads(files["package.json"])
    except ValueError:
        raise PolicyError("package_invalid") from None
    profile = json.loads((ROOT / "trusted-profile.json").read_text())
    require(
        sha(files["package-lock.json"]) == profile["lock_sha256"],
        "unreviewed_dependency_lock",
    )
    require(
        package.get("dependencies") == profile["dependencies"]
        and package.get("devDependencies") == profile["devDependencies"],
        "unreviewed_dependencies",
    )
    schema = files["prisma/schema.prisma"].decode()
    generators = re.findall(r"\bgenerator\s+\w+\s*\{([^}]*)\}", schema, re.S)
    require(
        len(generators) == 1
        and re.fullmatch(r'\s*provider\s*=\s*"prisma-client-js"\s*', generators[0])
        is not None,
        "unsupported_prisma_generators",
    )

    require(
        re.findall(r'provider\s*=\s*"([^"]+)"', schema)
        == ["prisma-client-js", "postgresql"],
        "unsupported_prisma_generators",
    )
    require(
        not re.search(r"\b(output|engineType|binaryTargets)\s*=", schema),
        "unsupported_prisma_output",
    )


@contextlib.contextmanager
def image_lock(image, *, timeout=900, on_wait=None):
    """Serialize tag creation across this user's local Builder processes."""
    require(
        isinstance(timeout, (int, float))
        and not isinstance(timeout, bool)
        and math.isfinite(timeout)
        and timeout >= 0,
        "invalid_build_lock_timeout",
    )
    directory = Path(tempfile.gettempdir()).resolve() / (
        "inframorph-builder-" + str(os.getuid())
    )
    directory.mkdir(mode=0o700, exist_ok=True)
    require(
        not directory.is_symlink()
        and directory.stat().st_uid == os.getuid()
        and directory.stat().st_mode & 0o077 == 0,
        "untrusted_build_lock",
    )
    fd = os.open(
        directory / (sha(image.encode()) + ".lock"),
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
        0o600,
    )
    try:
        deadline = time.monotonic() + timeout
        notified = False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeFailure("image_build_wait_timeout") from None
                if not notified and on_wait is not None:
                    on_wait()
                notified = True
                time.sleep(min(0.1, remaining))
        yield
    finally:
        os.close(fd)


def build(snapshot, bundle, plan, *, lock_timeout=900, **kwargs):
    plan = Plan.model_validate(plan)

    def waiting():
        emit(
            kwargs.get("sink"),
            kwargs.get("deployment_id", "local-build"),
            plan.target,
            "build",
            "started",
            detail="image_build_waiting",
        )

    with image_lock(plan.image_tag, timeout=lock_timeout, on_wait=waiting):
        return _build(snapshot, bundle, plan, **kwargs)


def _build(
    snapshot, bundle, plan, *, deployment_id="local-build", sink=None, execute=run
):
    plan = Plan.model_validate(plan)
    emit(sink, deployment_id, plan.target, "policy", "started")
    try:
        verified = validate_patch(snapshot, bundle, plan)
        check_build_profile(verified.files)
        emit(sink, deployment_id, plan.target, "policy", "ok")
    except (ValueError, OSError) as exc:
        emit(
            sink,
            deployment_id,
            plan.target,
            "policy",
            "fail",
            detail=failure_code(exc, "policy_or_build_profile_rejected"),
        )
        raise
    template = (ROOT / "templates/Dockerfile").read_bytes()
    labels = {
        "inframorph.source_revision": verified.source_revision,
        "inframorph.patched_digest": verified.patched_digest,
        "inframorph.builder_template": sha(template),
    }
    emit(sink, deployment_id, plan.target, "build", "started")
    try:
        execute(["docker", "info", "--format", "{{.OSType}}"])
        # List first: an inspect failure must not be mistaken for a missing image.
        exists = execute(["docker", "image", "ls", "--quiet", plan.image_tag])
        if exists:
            info = json.loads(execute(["docker", "image", "inspect", plan.image_tag]))[
                0
            ]
            require(
                all(
                    (info.get("Config", {}).get("Labels") or {}).get(k) == v
                    for k, v in labels.items()
                ),
                "image_tag_collision",
            )
        else:
            with tempfile.TemporaryDirectory(prefix="inframorph-build-") as directory:
                context = Path(directory)
                # Copy owned immutable bytes, never reread the now-mutable C artifact.
                write_files(context, verified.files)
                (context / "_inframorph.Dockerfile").write_bytes(template)
                (context / ".dockerignore").write_text(".git\n.env*\nnode_modules\n")
                args = [
                    "docker",
                    "buildx",
                    "build",
                    "--load",
                    "--platform",
                    "linux/amd64",
                    "-f",
                    str(context / "_inframorph.Dockerfile"),
                    "-t",
                    plan.image_tag,
                ]
                for k, v in labels.items():
                    args.extend(["--label", k + "=" + v])
                execute(args + [str(context)], timeout=900)
            info = json.loads(execute(["docker", "image", "inspect", plan.image_tag]))[
                0
            ]
        require(
            info["Os"] == "linux" and info["Architecture"] == "amd64",
            "image_platform_mismatch",
        )
        require(
            all(
                (info.get("Config", {}).get("Labels") or {}).get(k) == v
                for k, v in labels.items()
            ),
            "image_provenance_mismatch",
        )
        artifact = BuildArtifact(
            source_revision=plan.source_revision,
            target=plan.target,
            image=plan.image_tag,
            platform="linux/amd64",
        )
        emit(sink, deployment_id, plan.target, "build", "ok")
        return artifact
    except (ValueError, RuntimeError, KeyError) as exc:
        emit(
            sink,
            deployment_id,
            plan.target,
            "build",
            "fail",
            detail=failure_code(exc, "image_build_or_verification_failed"),
        )
        raise
