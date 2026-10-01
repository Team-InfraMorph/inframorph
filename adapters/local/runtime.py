"""Plan-driven local Compose deployment with private state and explicit rollback."""

import base64
import contextlib
import fcntl
import http.client
import json
import os
import re
import secrets
import shlex
import stat
import time
import urllib.error
import urllib.request
from pathlib import Path

from builder.runtime import RuntimeFailure, emit, failure_code, run
from policy_gate.gate import require
from schemas import BuildArtifact, Plan

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)


def private_file(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "unsafe_state_file")
        os.fchmod(fd, 0o600)
        os.ftruncate(fd, 0)
        with os.fdopen(fd, "w", closefd=False) as stream:
            stream.write(text)
    finally:
        os.close(fd)


def env_file(path, values):
    for key, value in values.items():
        require(
            re.fullmatch(r"[A-Z][A-Z0-9_]*", key) is not None
            and isinstance(value, str),
            "invalid_env",
        )
        require(not any(c in value for c in "\r\n'$"), "unsupported_env_characters")
    private_file(path, "".join(k + "='" + v + "'\n" for k, v in values.items()))


def service_command(command):
    require(isinstance(command, str), "worker_command_missing")
    parts = shlex.split(command)
    require(
        len(parts) == 2
        and parts[0] == "node"
        and re.fullmatch(r"src/[A-Za-z0-9_/-]+\.js", parts[1]) is not None
        and ".." not in parts[1].split("/"),
        "unsupported_worker_command",
    )
    return parts


def compose_document(plan, image_id, project, *, publish=False):
    plan = Plan.model_validate(plan)
    require(plan.target.value == "local", "local_target_required")
    require(
        re.fullmatch(r"inframorph-[a-z0-9-]+", project) is not None, "invalid_project"
    )
    require(
        re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is not None, "invalid_image_id"
    )
    require(
        1 <= len(plan.services) <= 4 and plan.db is not None,
        "unsupported_local_profile",
    )
    require(
        plan.storage is None or plan.storage.path.rstrip("/") == "uploads",
        "unsupported_storage_path",
    )
    require(not (set(plan.config) & set(plan.secrets)), "config_overrides_secret")
    require(
        all(not any(c in v for c in "$\r\n") for v in plan.config.values()),
        "unsafe_config_value",
    )
    web = next(spec for spec in plan.services if spec.public)
    services = {}
    services["db"] = {
        "image": "postgres:16-alpine",
        "env_file": ["db.env"],
        "volumes": ["db-data:/var/lib/postgresql/data"],
        "healthcheck": {
            "test": ["CMD", "pg_isready", "-U", "inframorph", "-d", "inframorph"],
            "interval": "2s",
            "timeout": "3s",
            "retries": 30,
        },
    }
    services["schema"] = {
        "image": image_id,
        "env_file": ["app.env"],
        "command": ["./node_modules/.bin/prisma", "db", "push", "--skip-generate"],
        "depends_on": {"db": {"condition": "service_healthy"}},
        "restart": "no",
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
    }
    for spec in plan.services:
        require(spec.name not in ("db", "schema", "tunnel"), "reserved_service_name")
        service = {
            "image": image_id,
            "env_file": ["app.env"],
            "environment": dict(plan.config),
            "depends_on": {"schema": {"condition": "service_completed_successfully"}},
            "read_only": True,
            "tmpfs": ["/tmp"],
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "mem_limit": str(spec.mem) + "m",
            "cpus": spec.cpu / 1024,
            "restart": "unless-stopped",
        }
        if plan.storage:
            service["volumes"] = ["uploads:/app/uploads"]
        if spec.kind.value == "http":
            require(spec.public, "only_public_http_supported")
            require(spec.command is None, "http_command_override_forbidden")
            service["environment"]["PORT"] = str(spec.port)
            service["ports"] = [f"127.0.0.1::{spec.port}"]
            url = "http://127.0.0.1:" + str(spec.port) + spec.health
            js = (
                "require('http').get("
                + json.dumps(url)
                + ",r=>{r.resume();process.exit(r.statusCode===200?0:1)})"
                + ".on('error',()=>process.exit(1)).setTimeout(5000,()=>process.exit(1))"
            )
            service["healthcheck"] = {
                "test": ["CMD", "node", "-e", js],
                "interval": "5s",
                "timeout": "15s",
                "start_period": "20s",
                "retries": 12,
            }
        else:
            service["command"] = service_command(spec.command)
        services[spec.name] = service
    if publish:
        services["tunnel"] = {
            "image": "cloudflare/cloudflared:latest",
            "command": [
                "tunnel",
                "--no-autoupdate",
                "--protocol",
                "http2",
                "--url",
                f"http://{web.name}:{web.port}",
            ],
            "depends_on": {web.name: {"condition": "service_healthy"}},
            "restart": "unless-stopped",
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
        }
    return {
        "services": services,
        "volumes": {
            "db-data": {"name": project + "-db"},
            "uploads": {"name": project + "-uploads"},
        },
    }


def request(url, *, data=None, content_type=None, expected=200):
    headers = {"Content-Type": content_type} if content_type else {}
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=data, headers=headers), timeout=10
        ) as response:
            require(response.status == expected, "smoke_http_status")
            return response.read(6 * 1024 * 1024)
    except (OSError, http.client.HTTPException):
        raise RuntimeFailure("smoke_http_failed") from None


def smoke(url, *, record=None):
    require(url.startswith(("http://127.0.0.1:", "https://")), "invalid_smoke_url")
    health = json.loads(request(url + "/health"))
    require(health.get("status") == "ok", "health_payload_failed")
    if record is None:
        text = "InfraMorph smoke " + secrets.token_hex(6)
        note = json.loads(
            request(
                url + "/api/notes",
                data=json.dumps({"text": text}).encode(),
                content_type="application/json",
                expected=201,
            )
        )
        image = json.loads(
            request(
                url + "/api/images", data=PNG, content_type="image/png", expected=201
            )
        )
        require(
            isinstance(image.get("url"), str)
            and re.fullmatch(r"/api/images/[A-Za-z0-9._-]+", image["url"]) is not None,
            "image_url_invalid",
        )
        record = {"note": note, "image_url": image["url"]}
    require(
        record["note"] in json.loads(request(url + "/api/notes")),
        "note_persistence_failed",
    )
    require(request(url + record["image_url"]) == PNG, "image_persistence_failed")
    return record


def wait_for(action, timeout=120):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return action()
        except (RuntimeError, ValueError, KeyError, OSError, IndexError):
            if time.monotonic() >= deadline:
                raise RuntimeFailure("health_or_tunnel_timeout") from None
            time.sleep(1)


@contextlib.contextmanager
def lock_state(state_dir):
    state_dir = Path(state_dir).absolute()
    require(
        not any(p.is_symlink() for p in (state_dir, *state_dir.parents)),
        "state_symlink_forbidden",
    )
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_dir.chmod(0o700)
    fd = os.open(state_dir / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeFailure("deployment_already_running") from None
        yield state_dir
    finally:
        os.close(fd)


def compose_args(state, config):
    path = Path(config).resolve()
    require(
        path.is_relative_to(state.resolve()) and path.name == "compose.json",
        "untrusted_compose_path",
    )
    return [
        "docker",
        "compose",
        "--project-name",
        "inframorph-" + state.name,
        "-f",
        str(path),
    ]


def local_url(execute, args, web):
    address = execute(args + ["port", web.name, str(web.port)]).splitlines()[0]
    require(
        re.fullmatch(r"127\.0\.0\.1:[0-9]+", address) is not None,
        "unexpected_public_port",
    )
    return "http://" + address


def deploy(
    plan,
    artifact,
    state_dir,
    *,
    secret_values=None,
    publish=False,
    deployment_id="local-deploy",
    sink=None,
    execute=run,
):
    plan = Plan.model_validate(plan)
    artifact = BuildArtifact.model_validate(artifact)
    require(
        plan.target.value == artifact.target.value == "local"
        and plan.source_revision == artifact.source_revision
        and plan.image_tag == artifact.image,
        "artifact_plan_mismatch",
    )
    web = next(s for s in plan.services if s.public)
    with lock_state(state_dir) as state:
        require(state.name == plan.app, "state_app_mismatch")
        current = state / "current.json"
        previous = json.loads(current.read_text()) if current.exists() else None
        # Pin image ID so subsequent mutable-tag changes cannot affect deployment/rollback.
        info = json.loads(execute(["docker", "image", "inspect", artifact.image]))[0]
        labels = info.get("Config", {}).get("Labels") or {}
        require(
            info["Os"] == "linux"
            and info["Architecture"] == "amd64"
            and labels.get("inframorph.source_revision") == plan.source_revision
            and re.fullmatch(
                "[0-9a-f]{64}", labels.get("inframorph.patched_digest", "")
            )
            is not None,
            "unverified_image",
        )
        secrets_path = state / "db-password"
        password = (
            secrets_path.read_text()
            if secrets_path.exists()
            else secrets.token_urlsafe(32)
        )
        if not secrets_path.exists():
            private_file(secrets_path, password)
        directory = state / (str(time.time_ns()) + "-" + secrets.token_hex(3))
        directory.mkdir(mode=0o700)
        env = dict(secret_values or {})
        require("DATABASE_URL" not in env, "local_database_url_is_managed")
        require(
            set(env) == set(plan.secrets) - {"DATABASE_URL"}, "missing_or_extra_secrets"
        )
        env["DATABASE_URL"] = (
            "postgresql://inframorph:" + password + "@db:5432/inframorph"
        )
        env_file(directory / "app.env", env)
        env_file(
            directory / "db.env",
            {
                "POSTGRES_USER": "inframorph",
                "POSTGRES_DB": "inframorph",
                "POSTGRES_PASSWORD": password,
            },
        )
        project = "inframorph-" + plan.app
        document = compose_document(plan, info["Id"], project, publish=publish)
        config = directory / "compose.json"
        private_file(config, json.dumps(document, indent=2))
        args = compose_args(state, config)
        emit(sink, deployment_id, "local", "start", "started")
        try:
            execute(args + ["config", "--quiet"])
            execute(args + ["up", "-d", "--remove-orphans"], timeout=300)
            url = wait_for(lambda: local_url(execute, args, web))
            emit(sink, deployment_id, "local", "health", "started")
            wait_for(lambda: request(url + web.health), timeout=120)
            record = smoke(url)
            if previous:
                smoke(url, record=previous["record"])
            emit(sink, deployment_id, "local", "health", "ok")
            emit(sink, deployment_id, "local", "smoke", "ok")
            public_url = None
            if publish:

                def find_tunnel():
                    logs = execute(args + ["logs", "--no-color", "tunnel"])
                    found = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", logs)
                    require(bool(found), "tunnel_pending")
                    return found[-1]

                public_url = wait_for(find_tunnel)
                wait_for(lambda: smoke(public_url, record=record))
            result = {
                "config": str(config),
                "image_id": info["Id"],
                "artifact": artifact.model_dump(mode="json"),
                "url": url,
                "public_url": public_url,
                "record": record,
                "plan": plan.model_dump(mode="json"),
            }
            if previous:
                private_file(state / "previous.json", json.dumps(previous, indent=2))
            temporary = state / "current.next"
            private_file(temporary, json.dumps(result, indent=2))
            temporary.replace(current)
            emit(sink, deployment_id, "local", "start", "ok")
            emit(sink, deployment_id, "local", "url", "ok", url=public_url or url)
            return result
        except (RuntimeError, ValueError, KeyError, OSError, IndexError) as exc:
            # No raw app logs in events: they may contain secrets. Fixed failure code for C/D.
            emit(
                sink,
                deployment_id,
                "local",
                "start",
                "fail",
                detail=failure_code(exc, "local_deploy_or_smoke_failed"),
            )
            if previous:
                try:
                    emit(sink, deployment_id, "local", "rollback", "started")
                    restore_args = compose_args(state, previous["config"])
                    execute(
                        restore_args + ["up", "-d", "--remove-orphans"], timeout=300
                    )
                    old_plan = Plan.model_validate(previous["plan"])
                    old_web = next(s for s in old_plan.services if s.public)
                    previous["url"] = wait_for(
                        lambda: local_url(execute, restore_args, old_web)
                    )
                    wait_for(lambda: smoke(previous["url"], record=previous["record"]))
                    previous["public_url"] = None
                    private_file(state / "current.next", json.dumps(previous, indent=2))
                    (state / "current.next").replace(current)
                    emit(
                        sink,
                        deployment_id,
                        "local",
                        "rollback",
                        "ok",
                        url=previous["url"],
                    )
                except (RuntimeError, ValueError, OSError):
                    emit(
                        sink,
                        deployment_id,
                        "local",
                        "rollback",
                        "fail",
                        detail="restore_failed",
                    )
            else:
                # A failed first deployment has no previous Compose to restore.
                # Stop the public tunnel and app, but preserve named data volumes.
                emit(
                    sink,
                    deployment_id,
                    "local",
                    "rollback",
                    "started",
                    detail="initial_deployment_cleanup",
                )
                try:
                    execute(args + ["down", "--remove-orphans"], timeout=120)
                except (RuntimeError, OSError):
                    emit(
                        sink,
                        deployment_id,
                        "local",
                        "rollback",
                        "fail",
                        detail="initial_deployment_cleanup_failed",
                    )
                    raise RuntimeFailure("initial_deployment_cleanup_failed") from exc
                emit(
                    sink,
                    deployment_id,
                    "local",
                    "rollback",
                    "ok",
                    detail="initial_deployment_cleaned",
                )
            raise


def rollback(state_dir, *, deployment_id="local-rollback", sink=None, execute=run):
    with lock_state(state_dir) as state:
        require(
            (state / "previous.json").is_file() and (state / "current.json").is_file(),
            "no_rollback_point",
        )
        previous = json.loads((state / "previous.json").read_text())
        current = json.loads((state / "current.json").read_text())
        args = compose_args(state, previous["config"])
        plan = Plan.model_validate(previous["plan"])
        emit(sink, deployment_id, "local", "rollback", "started")
        try:
            execute(args + ["up", "-d", "--remove-orphans"], timeout=300)
            web = next(s for s in plan.services if s.public)
            previous["url"] = wait_for(lambda: local_url(execute, args, web))
            wait_for(lambda: smoke(previous["url"], record=current["record"]))
            wait_for(lambda: smoke(previous["url"], record=previous["record"]))
            previous["public_url"] = (
                None  # Quick Tunnel URL is ephemeral; never return a stale one.
            )
            private_file(state / "previous.json", json.dumps(current, indent=2))
            private_file(state / "current.next", json.dumps(previous, indent=2))
            (state / "current.next").replace(state / "current.json")
            emit(sink, deployment_id, "local", "rollback", "ok", url=previous["url"])
            return previous
        except (ValueError, RuntimeError):
            emit(
                sink,
                deployment_id,
                "local",
                "rollback",
                "fail",
                detail="rollback_or_persistence_check_failed",
            )
            raise
