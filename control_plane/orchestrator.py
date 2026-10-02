"""배포기(별도 프로세스)를 실행하고 stdout의 DeployEvent JSONL을 검증·저장한다.

규칙(schemas/README.md): stdout은 DeployEvent JSONL 전용, 진단 문구는 stderr, 종료 코드 0이 성공.
"""
import logging
import os
import re
import subprocess
import threading
import queue
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic import ValidationError

from schemas.events import DeployEvent

from .db import Status
from analyzer.local_verify import child_environment

log = logging.getLogger("control_plane.orchestrator")

DEFAULT_TIMEOUT_S = float(os.environ.get("INFRAMORPH_DEPLOY_TIMEOUT", "900"))

_SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)=\S+"),
    re.compile(r"(?i)postgres(?:ql)?://[^\s:@]+:[^\s@]+@"),
]


def mask_secrets(text):
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("***", text)
    return text


@dataclass
class RunResult:
    exit_code: int | None
    accepted: int
    rejected: int
    status: Status
    url: str | None = None


def _final_status(exit_code, events):
    if any(e.step == "rollback" and e.status == "ok" for e in events):
        return Status.ROLLED_BACK
    if exit_code != 0 or any(e.status == "fail" for e in events):
        return Status.FAILED
    return Status.LIVE


def _overall_status(statuses):
    """대상 하나라도 실패하면 FAILED, 롤백됐으면 ROLLED_BACK, 모두 성공해야 LIVE."""
    for status in (Status.FAILED, Status.ROLLED_BACK):
        if status in statuses:
            return status
    return Status.LIVE


# Only the trusted AWS adapter needs cloud configuration. Builders (even for AWS)
# and C/E workers retain the minimal environment.
AWS_ADAPTER_ENV = frozenset({
    "INFRAMORPH_FOUNDATION_OUTPUTS", "INFRAMORPH_AWS_ACCOUNT_ID",
    "INFRAMORPH_TF_STATE_BUCKET", "INFRAMORPH_MIGRATION_COMMAND",
    "INFRAMORPH_DEPLOY_TIMEOUT_SECONDS", "AWS_PROFILE", "AWS_DEFAULT_PROFILE",
    "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_CONFIG_FILE",
    "AWS_SHARED_CREDENTIALS_FILE", "AWS_CA_BUNDLE", "AWS_SDK_LOAD_CONFIG",
    "AWS_ROLE_ARN", "AWS_ROLE_SESSION_NAME", "AWS_WEB_IDENTITY_TOKEN_FILE",
})


def module_environment(target, cmd):
    env = child_environment()
    if (target == "aws" and len(cmd) >= 4
            and list(cmd[1:3]) == ["-m", "adapters.aws"]
            and cmd[3] in {"deploy", "rollback"}):
        env.update({key: os.environ[key] for key in AWS_ADAPTER_ENV if key in os.environ})
    return env


def stream_events(store, deployment_id, target, cmd, timeout=DEFAULT_TIMEOUT_S, cwd=None):
    """cmd를 실행해 stdout의 DeployEvent를 줄마다 검증·저장한다. 반환 = (종료 코드, 받은 이벤트, 버린 줄 수).

    배포기뿐 아니라 이벤트를 내는 다른 모듈(E Builder 등)도 같은 규칙으로 받는다.
    """
    rejected, events = 0, []
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, env=module_environment(target, cmd), cwd=cwd)
    except OSError as exc:
        log.error("%s command failed to start: %s", target, exc)
        return None, events, rejected

    inbox = queue.Queue(maxsize=64)
    stopped = threading.Event()

    def put(value):
        while not stopped.is_set():
            try:
                inbox.put(value, timeout=0.1)
                return
            except queue.Full:
                pass

    def read_lines():
        try:
            while not stopped.is_set():
                line = proc.stdout.readline(65_537)
                if not line:
                    break
                put(line)
                if len(line) > 65_536:
                    break
        finally:
            put(None)

    reader = threading.Thread(target=read_lines, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(cmd, timeout)
            try:
                line = inbox.get(timeout=remaining)
            except queue.Empty:
                raise subprocess.TimeoutExpired(cmd, timeout) from None
            if line is None:
                break
            if len(line) > 65_536 or len(events) + rejected >= 2048:
                raise subprocess.TimeoutExpired(cmd, timeout)
            line = line.strip()
            if not line:
                continue
            try:
                event = DeployEvent.model_validate_json(line)
            except ValidationError as exc:
                rejected += 1
                log.warning("rejected event line: %s", exc.errors()[0]["msg"])
                continue
            if event.deployment_id != deployment_id or event.target.value != target:
                rejected += 1
                log.warning("rejected event for %s/%s", event.deployment_id, event.target.value)
                continue
            if event.detail:
                event = event.model_copy(update={"detail": mask_secrets(event.detail)})
            store.add_event(deployment_id, event.model_dump(mode="json", exclude_none=True))
            events.append(event)
        exit_code = proc.wait(timeout=max(0.01, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        # C's SIGTERM handler cancels E and stops only its Compose namespace.
        proc.terminate()
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        exit_code = None
        log.error("%s command timed out after %ss", target, timeout)
    finally:
        stopped.set()
        reader.join(timeout=1)
        if not reader.is_alive():
            proc.stdout.close()
    return exit_code, events, rejected


def run_target(store, deployment_id, target, cmd, timeout=DEFAULT_TIMEOUT_S, cwd=None):
    """대상 하나의 배포기를 실행하고 끝날 때까지 이벤트를 저장한 뒤 그 대상의 상태를 기록한다."""
    exit_code, events, rejected = stream_events(store, deployment_id, target, cmd, timeout, cwd)
    status = _final_status(exit_code, events)  # 시작 실패·시간 초과(None)도 0이 아니므로 FAILED
    url = next((e.url for e in reversed(events) if e.url and e.status == "ok"), None)
    store.set_target_status(deployment_id, target, status, url)
    return RunResult(exit_code, len(events), rejected, status, url)


def record_verification(store, deployment_id, target, check, event=False):
    """직접 확인 결과를 저장하고, 배포 직후 확인이면 타임라인에도 '상태 확인' 줄로 남긴다(확인 안 함은 남기지 않음)."""
    store.set_verification(deployment_id, target, check)
    if event and check["status"] != "skipped":
        ok = check["status"] == "ok"
        detail = (f"조종실 직접 확인: {check['code']} · {check['ms'] / 1000:.2f}초" if ok
                  else f"조종실 직접 확인 실패: {check['detail']}")
        store.add_event(deployment_id, DeployEvent(
            deployment_id=deployment_id, ts=datetime.now(timezone.utc), target=target, step="health",
            status="ok" if ok else "fail", detail=detail, url=check["url"]).model_dump(mode="json", exclude_none=True))


def run_deployment(store, deployment_id, cmds, timeout=DEFAULT_TIMEOUT_S, verify=None):
    """대상별 배포기를 동시에 실행한다(기획서 시나리오 A: Local과 AWS 동시 진행).

    cmds = {target: cmd} 또는 {target: (작업 위치, cmd)}. 팀원 모듈은 자기 checkout에서 실행해야 한다.
    """
    results = {}

    def run(target, spec):
        cwd, cmd = spec if isinstance(spec, tuple) else (None, spec)
        results[target] = run_target(store, deployment_id, target, cmd, timeout, cwd)

    threads = [threading.Thread(target=run, args=item) for item in cmds.items()]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if verify is not None:
        for target, result in results.items():
            if result.url and result.status in (Status.LIVE, Status.ROLLED_BACK):
                record_verification(store, deployment_id, target, verify(target, result.url), event=True)
    status = _overall_status({r.status for r in results.values()})
    store.set_status(deployment_id, status)
    return results
