"""C recovery callbacks backed by E's real modules in an assembled team checkout.

The B Planner is an explicit callback. No fixture Planner or implicit approval
is supplied here. Namespace and publication are explicit operator-owned options.
"""
import asyncio
import json
import os
from pathlib import Path
import re
import signal
import sys

from schemas import Plan
from .feedback import LocalFailure
from .local_verify import child_environment
from .recovery import Approval, BuiltPatch, LocalCheck, RecoveryHooks


ROOT = Path(__file__).resolve().parents[1]
MAX_REPLY = 512_000


class EWorkerError(ValueError):
    """Sanitized E diagnostic carried across the private worker protocol."""


def classify_e_failure(code):
    # These codes identify completed application checks. A generic HTTP error,
    # timeout, daemon/Compose error or cleanup failure has no proven app cause.
    known = {"health_payload_failed": ("health", "health_unhealthy"),
             "note_persistence_failed": ("smoke", "note_roundtrip_failed"),
             "image_persistence_failed": ("smoke", "image_readback_failed")}
    stage, reason = known.get(code, ("infra", "unknown"))
    return LocalFailure(stage=stage, code=reason)


async def run_worker(payload, *, module="analyzer.e_worker"):
    """No shell; a separate process makes E's blocking Docker calls cancellable."""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", module, cwd=ROOT, env=child_environment(),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True)

    async def bounded_read():
        chunks, size = [], 0
        while chunk := await process.stdout.read(16_384):
            size += len(chunk)
            if size > MAX_REPLY:
                raise ValueError("e_reply_limit")
            chunks.append(chunk)
        await process.wait()
        return b"".join(chunks)

    reader = asyncio.create_task(bounded_read())
    try:
        process.stdin.write(json.dumps(payload).encode())
        await process.stdin.drain()
        process.stdin.close()
        data = await reader
        reply = json.loads(data)
        if not isinstance(reply, dict) or type(reply.get("ok")) is not bool:
            raise ValueError("invalid_e_reply")
        if not reply["ok"]:
            code = reply.get("code", "e_runtime_failed")
            if not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,80}", code):
                code = "e_runtime_failed"
            if payload["action"] != "deploy":
                raise EWorkerError(code)
        elif process.returncode != 0:
            raise ValueError("e_worker_failed")
        return reply
    finally:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


class EConnector:
    def __init__(self, *, snapshot, repo_map, state_root, runtime_name, deployment_id, make_plan, worker=None, publish=False):
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,62}", runtime_name):
            raise ValueError("invalid_runtime_namespace")
        self.snapshot = Path(snapshot).absolute()
        self.mapping = repo_map
        self.state = Path(state_root).absolute() / runtime_name
        if (self.state == self.snapshot or self.snapshot in self.state.parents or
                any(p.is_symlink() for p in (self.state, *self.state.parents))):
            raise ValueError("invalid_runtime_state")
        self.runtime_name = runtime_name
        self.deployment_id = deployment_id
        self.make_plan = make_plan
        if type(publish) is not bool:
            raise ValueError("invalid_publish_option")
        self.publish = publish
        self.worker = worker
        self.last_deployment = None

    async def invoke(self, payload):
        return await (self.worker or run_worker)(payload)

    def payload(self, action, **values):
        return {"action": action, "snapshot": str(self.snapshot),
                "repo_map": self.mapping.model_dump(mode="json"), "state": str(self.state),
                "runtime_name": self.runtime_name, "deployment_id": self.deployment_id, **values}

    async def validate_intent(self, intent, signature):
        await self.invoke(self.payload("intent", intent=intent.model_dump(mode="json")))
        return Approval(approved=True, fingerprint=signature)

    async def validate_patch(self, candidate, signature):
        await self.invoke(self.payload("patch", bundle=str(candidate.directory),
                                      plan=candidate.plan.model_dump(mode="json")))
        return Approval(approved=True, fingerprint=signature)

    async def build(self, candidate, signature):
        reply = await self.invoke(self.payload("build", bundle=str(candidate.directory),
                                             plan=candidate.plan.model_dump(mode="json")))
        return BuiltPatch(artifact=reply["artifact"], fingerprint=signature)

    async def cleanup(self):
        async with asyncio.timeout(60):
            return await self.invoke(self.payload("cleanup"))

    async def check_local(self, artifact, plan):
        # Plan.app is the logical source app. E uses it for physical Compose and
        # volume names; use a caller-owned stable project namespace at that boundary.
        runtime_plan = Plan.model_validate(plan.model_dump() | {"app": self.runtime_name})
        try:
            reply = await self.invoke(self.payload("deploy", plan=runtime_plan.model_dump(mode="json"),
                                                 artifact=artifact.model_dump(mode="json"), publish=self.publish))
        except BaseException:
            # The E process may have died between `up` and writing current.json.
            # Stop only this namespace; preserve volumes and never prune Docker.
            task = asyncio.create_task(self.cleanup())
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
            raise
        if not reply["ok"]:
            return LocalCheck(ok=False, failure=classify_e_failure(reply["code"]))
        self.last_deployment = reply["deployment"]
        return LocalCheck(ok=True, url=reply["deployment"]["url"])

    def hooks(self):
        return RecoveryHooks(self.validate_intent, self.make_plan, self.validate_patch,
                             self.build, self.check_local)
