"""Private D deployment process: Local 테스트를 통과한 바로 그 이미지를 사내 서버(원격 Docker)에 배포한다.

- 다시 빌드하지 않는다. 맥북에서 검증된 이미지를 `docker save | docker load`로 옮기고 레이어·출처 라벨이 같은지 확인한다.
- Compose 구성은 E의 compose_document를 그대로 쓰고, 앱 포트만 사내망 주소에 연다.
- 조종실이 사내 서버 주소로 직접 노트·이미지를 쓰고 읽어 확인한다. 재배포 때는 이전 데이터가 남았는지도 본다.
stdout = DeployEvent JSONL(target=onprem), 실패는 고정 코드만 남긴다.
"""
import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time

from adapters.local.runtime import PNG, compose_document, env_file, private_file, request
from analyzer.local_verify import child_environment
from schemas import DeployEvent, Plan

from .db import Status, Store
from .local_deploy import load_context
from .onprem_config import load_config


class OnpremFailure(Exception):
    """화면에 그대로 보여도 되는 고정 실패 코드."""


def emit(context, step, status, detail=None, url=None, duration_ms=None):
    event = DeployEvent(deployment_id=context.deployment_id, ts=datetime.now(timezone.utc), target="onprem",
                        step=step, status=status, detail=detail, url=url, duration_ms=duration_ms)
    sys.stdout.write(event.jsonl())
    sys.stdout.flush()


def environments(config):
    local = child_environment()
    remote = dict(local, DOCKER_HOST=config.docker_host)
    if "SSH_AUTH_SOCK" in os.environ:
        remote["SSH_AUTH_SOCK"] = os.environ["SSH_AUTH_SOCK"]
    return local, remote


def docker(args, env, *, timeout=120, code="onprem_docker_failed", stdin=None):
    try:
        result = subprocess.run(["docker", *args], env=env, capture_output=True, text=True,
                                timeout=timeout, stdin=stdin)
    except (OSError, subprocess.SubprocessError):
        raise OnpremFailure(code) from None
    if result.returncode:
        raise OnpremFailure(code)
    return result.stdout.strip()


def image_info(tag, env, code):
    info = json.loads(docker(["image", "inspect", tag], env, code=code))[0]
    return info["Id"], info


def fingerprint(info):
    """이미지 내용의 동일성. 이미지 ID는 저장 방식(overlay2 / containerd)마다 다르게 매겨지므로
    실제 내용인 레이어 해시와 Builder가 붙인 출처 라벨(커밋·패치 해시)로 비교한다."""
    labels = info.get("Config", {}).get("Labels") or {}
    return (info.get("Os"), info.get("Architecture"), tuple(info.get("RootFS", {}).get("Layers") or ()),
            labels.get("inframorph.source_revision"), labels.get("inframorph.patched_digest"))


def transfer_image(tag, local, remote, timeout):
    """맥북의 검증된 이미지를 사내 서버로 옮긴다. 같은 내용이 이미 있으면 건너뛴다.
    반환 = (사내 서버의 이미지 ID, 출처 라벨, 전송 시간 ms 또는 생략이면 None)."""
    _, info = image_info(tag, local, "onprem_local_image_missing")
    expected = fingerprint(info)
    labels = info.get("Config", {}).get("Labels") or {}
    try:
        remote_id, remote_info = image_info(tag, remote, "onprem_remote_inspect")
        if fingerprint(remote_info) == expected:
            return remote_id, labels, None
    except OnpremFailure:
        pass
    started = time.monotonic()
    try:
        # docker load는 gzip 압축본을 그대로 받는다. 압축해서 보내 사내망 전송량을 줄인다.
        save = subprocess.Popen(["docker", "save", tag], env=local, stdout=subprocess.PIPE)
        pack = subprocess.Popen(["gzip", "-1"], stdin=save.stdout, stdout=subprocess.PIPE)
        save.stdout.close()
        load = subprocess.run(["docker", "load"], env=remote, stdin=pack.stdout, capture_output=True, timeout=timeout)
        pack.stdout.close()
        save.wait(timeout=30)
        pack.wait(timeout=30)
    except (OSError, subprocess.SubprocessError):
        raise OnpremFailure("onprem_image_transfer_failed") from None
    if save.returncode or pack.returncode or load.returncode:
        raise OnpremFailure("onprem_image_transfer_failed")
    remote_id, remote_info = image_info(tag, remote, "onprem_image_transfer_failed")
    if fingerprint(remote_info) != expected:
        raise OnpremFailure("onprem_image_content_mismatch")
    return remote_id, labels, int((time.monotonic() - started) * 1000)


def onprem_document(plan, image_id, project, bind):
    """E의 Local Compose 구성에서 앱 포트를 여는 주소만 바꾼다(127.0.0.1 → 사내망)."""
    document = compose_document(plan, image_id, project)
    for service in document["services"].values():
        if "ports" in service:
            service["ports"] = [re.sub(r"^127\.0\.0\.1:", bind + ":", p) for p in service["ports"]]
    return document


def wait_for(action, timeout, code):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return action()
        except (RuntimeError, ValueError, KeyError, OSError, IndexError, OnpremFailure):
            if time.monotonic() >= deadline:
                raise OnpremFailure(code) from None
            time.sleep(1)


def smoke(url, record=None):
    """조종실이 사내 서버 주소로 직접 노트·이미지를 쓰고 읽는다. record가 있으면 이전 데이터가 남았는지 본다."""
    health = json.loads(request(url + "/health"))
    if health.get("status") != "ok":
        raise OnpremFailure("onprem_health_payload_failed")
    if record is None:
        note = json.loads(request(url + "/api/notes", data=json.dumps({"text": "InfraMorph 사내 서버 확인 " +
                          secrets.token_hex(4)}).encode(), content_type="application/json", expected=201))
        image = json.loads(request(url + "/api/images", data=PNG, content_type="image/png", expected=201))
        if not re.fullmatch(r"/api/images/[A-Za-z0-9._-]+", str(image.get("url"))):
            raise OnpremFailure("onprem_image_url_invalid")
        record = {"note": note, "image_url": image["url"]}
    if record["note"] not in json.loads(request(url + "/api/notes")):
        raise OnpremFailure("onprem_note_persistence_failed")
    if request(url + record["image_url"]) != PNG:
        raise OnpremFailure("onprem_image_persistence_failed")
    return record


def deploy(context, store, config):
    deployment = store.get_deployment(context.deployment_id)
    if (deployment is None or deployment["status"] != Status.DEPLOYING.value or
            "onprem" not in context.targets or "local" not in context.targets or
            deployment["targets"].get("local", {}).get("status") != Status.LIVE.value):
        # Local 테스트를 통과한 배포만 사내 서버로 간다.
        raise OnpremFailure("onprem_requires_passed_local_test")
    plan = Plan.model_validate(store.get_plans(context.deployment_id).get("local") or context.plan.model_dump(mode="json"))
    local, remote = environments(config)
    state = Path(context.state_root) / context.project_id / "onprem"
    if any(p.is_symlink() for p in (state, *state.parents)):
        raise OnpremFailure("onprem_state_symlink")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)

    step = "push"
    try:
        emit(context, step, "started", f"Local 테스트를 통과한 이미지를 {config.label}로 전송")
        image_id, labels, took = transfer_image(plan.image_tag, local, remote, config.timeout_seconds)
        if labels.get("inframorph.source_revision") != plan.source_revision:
            raise OnpremFailure("onprem_unverified_image")
        emit(context, step, "ok", "이미 같은 이미지가 있어 전송 생략" if took is None else
             f"이미지 전송 완료 · {image_id[7:19]}", duration_ms=took)

        step = "start"
        emit(context, step, "started", f"{config.label}에서 컨테이너 실행 (DB·앱)")
        password_file = state / "db-password"
        if not password_file.exists():
            private_file(password_file, secrets.token_urlsafe(32))
        password = password_file.read_text()
        if set(plan.secrets) - {"DATABASE_URL"}:
            raise OnpremFailure("onprem_secrets_unsupported")
        directory = state / (str(time.time_ns()) + "-" + secrets.token_hex(3))
        directory.mkdir(mode=0o700)
        env_file(directory / "app.env", {"DATABASE_URL": "postgresql://inframorph:" + password + "@db:5432/inframorph"})
        env_file(directory / "db.env", {"POSTGRES_USER": "inframorph", "POSTGRES_DB": "inframorph",
                                        "POSTGRES_PASSWORD": password})
        # 프로젝트마다 컨테이너·DB 볼륨을 분리한다(앱 이름이 같아도 다른 프로젝트의 데이터를 덮지 않게).
        project = "inframorph-onprem-" + context.project_id
        config_path = directory / "compose.json"
        private_file(config_path, json.dumps(onprem_document(plan, image_id, project, config.bind), indent=2))
        args = ["compose", "--project-name", project, "-f", str(config_path)]
        docker(args + ["config", "--quiet"], remote, code="onprem_compose_invalid")
        docker(args + ["up", "-d", "--remove-orphans"], remote, timeout=config.timeout_seconds, code="onprem_compose_up_failed")
        web = next(s for s in plan.services if s.public)
        address = wait_for(lambda: docker(args + ["port", web.name, str(web.port)], remote).splitlines()[0], 60,
                           "onprem_port_unavailable")
        port = address.rsplit(":", 1)[1]
        if not port.isdigit():
            raise OnpremFailure("onprem_port_unavailable")
        url = f"http://{config.host}:{port}"

        step = "health"
        emit(context, step, "started", f"{config.label} 앱 응답 대기")
        wait_for(lambda: request(url + web.health), 180, "onprem_health_timeout")
        current = state / "current.json"
        previous = json.loads(current.read_text()) if current.exists() else None
        record = smoke(url)
        kept = previous is not None and previous.get("record") is not None
        if kept:
            smoke(url, record=previous["record"])
        emit(context, step, "ok", f"{config.label} 응답 정상")
        emit(context, "smoke", "ok", "노트·이미지 쓰기·읽기 확인" + (" · 이전 배포 데이터 유지 확인" if kept else ""))
        private_file(state / "current.next", json.dumps({"config": str(config_path), "image_id": image_id,
                                                         "url": url, "record": record}, indent=2))
        (state / "current.next").replace(current)
        emit(context, "start", "ok", f"{config.label} 실행 중")
        emit(context, "url", "ok", f"{config.label} 접속 주소", url=url)
        return 0
    except OnpremFailure as error:
        emit(context, step, "fail", str(error))
        return 1
    except Exception:
        emit(context, step, "fail", "onprem_pipeline_failed")
        return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--onprem-config", required=True)
    args = parser.parse_args(argv)
    context = load_context(args.context)
    config = load_config(Path(args.onprem_config).absolute())
    return deploy(context, Store(Path(args.database)), config)


if __name__ == "__main__":
    raise SystemExit(main())
