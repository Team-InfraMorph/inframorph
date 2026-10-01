"""배포기(별도 프로세스)를 실행하고 stdout의 DeployEvent JSONL을 검증·저장한다.

규칙(schemas/README.md): stdout은 DeployEvent JSONL 전용, 진단 문구는 stderr, 종료 코드 0이 성공.
"""
import logging
import os
import re
import subprocess
from dataclasses import dataclass

from pydantic import ValidationError

from schemas.events import DeployEvent

from .db import Status

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


def _final_status(exit_code, events):
    if any(e.step == "rollback" and e.status == "ok" for e in events):
        return Status.ROLLED_BACK
    if exit_code != 0 or any(e.status == "fail" for e in events):
        return Status.FAILED
    return Status.LIVE


def run_deployer(store, deployment_id, cmd, timeout=DEFAULT_TIMEOUT_S):
    """cmd를 실행하고 끝날 때까지 이벤트를 저장한 뒤 최종 상태를 기록한다."""
    accepted, rejected, events = 0, 0, []
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, bufsize=1)
    except OSError as exc:
        log.error("deployer failed to start: %s", exc)
        store.set_status(deployment_id, Status.FAILED)
        return RunResult(None, 0, 0, Status.FAILED)

    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = DeployEvent.model_validate_json(line)
            except ValidationError as exc:
                rejected += 1
                log.warning("rejected event line: %s", exc.errors()[0]["msg"])
                continue
            if event.deployment_id != deployment_id:
                rejected += 1
                log.warning("rejected event for other deployment: %s", event.deployment_id)
                continue
            if event.detail:
                event = event.model_copy(update={"detail": mask_secrets(event.detail)})
            store.add_event(deployment_id, event.model_dump(mode="json", exclude_none=True))
            events.append(event)
            accepted += 1
        exit_code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        exit_code = None
        log.error("deployer timed out after %ss", timeout)
    finally:
        proc.stdout.close()

    status = _final_status(exit_code, events)
    store.set_status(deployment_id, status)
    return RunResult(exit_code, accepted, rejected, status)
