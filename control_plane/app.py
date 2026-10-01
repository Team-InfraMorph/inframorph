"""Control Plane API (구간 1·13·15).

실행: uvicorn control_plane.app:app --reload --port 8000  → http://localhost:8000/docs
"""
import asyncio
import json
import os
import re
import sys
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from schemas.common import Target

from . import webhook
from .db import TERMINAL, ConflictError, Status, Store
from .change_detector import plan_redeploy
from .orchestrator import run_deployment

DEFAULT_DB = Path(os.environ.get("INFRAMORPH_HOME", "/tmp/inframorph")) / "control_plane.db"
REPO_URL = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?(\.git)?/?")
BRANCH = re.compile(r"[A-Za-z0-9._/-]{1,100}")
SSE_POLL_S = 0.5


class ProjectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_url: str
    branch: str = "main"
    targets: list[Target] = Field(min_length=1)

    @field_validator("repo_url")
    @classmethod
    def github_https_only(cls, value):
        if not REPO_URL.fullmatch(value):
            raise ValueError("https://github.com/<owner>/<repo> 형식만 지원합니다")
        return value

    @field_validator("branch")
    @classmethod
    def safe_branch(cls, value):
        if not BRANCH.fullmatch(value) or ".." in value or value.startswith(("/", "-")) or value.endswith("/"):
            raise ValueError("허용되지 않는 브랜치 이름입니다")
        return value

    @field_validator("targets")
    @classmethod
    def unique_targets(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("배포 대상이 중복되었습니다")
        return value


def _fake_env(name, target, default=None):
    """INFRAMORPH_FAKE_<NAME>_<TARGET>이 있으면 그 대상에만, 없으면 INFRAMORPH_FAKE_<NAME>을 쓴다."""
    return os.environ.get(f"INFRAMORPH_FAKE_{name}_{target.upper()}", os.environ.get(f"INFRAMORPH_FAKE_{name}", default))


def fake_deployer_cmd(deployment_id, target):
    """실제 배포기가 붙기 전까지 쓰는 명령. 환경 변수로 지연·fixture·종료 코드를 대상별로 바꿀 수 있다."""
    cmd = [sys.executable, "-m", "control_plane.fake_deployer", "--deployment-id", deployment_id,
           "--target", target,
           "--delay", _fake_env("DELAY", target, "1"),
           "--exit-code", _fake_env("EXIT_CODE", target, "0")]
    if fixture := _fake_env("FIXTURE", target):
        cmd += ["--fixture", fixture]
    return cmd


def _sse(event, data, seq=None):
    head = f"id: {seq}\n" if seq is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def create_app(db_path=None, deployer_cmd=fake_deployer_cmd):
    store = Store(db_path or DEFAULT_DB)
    store.recover_interrupted()

    def execute(project_id, deployment_id):
        """대상별 배포기를 동시에 돌리고, 끝나면 그동안 쌓인 요청(queued) 중 최신 것을 이어서 실행한다."""
        while deployment_id:
            targets = store.get_deployment(deployment_id)["targets"]
            run_deployment(store, deployment_id, {t: deployer_cmd(deployment_id, t) for t in targets})
            deployment_id = None
            if store.has_queued(project_id):
                try:
                    deployment_id = store.begin_deploy(project_id)
                except ConflictError:
                    return

    app = FastAPI(title="InfraMorph Control Plane")
    app.state.store = store
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.post("/api/projects", status_code=201)
    def create_project(body: ProjectIn):
        targets = [t.value for t in body.targets]
        return store.create_project(body.repo_url, body.branch, targets, webhook.normalize_repo_url(body.repo_url))

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str):
        project = store.get_project(project_id)
        if project is None:
            raise HTTPException(404, "project not found")
        return project

    @app.post("/api/projects/{project_id}/deploy", status_code=202)
    def deploy(project_id: str, background: BackgroundTasks):
        if store.get_project(project_id) is None:
            raise HTTPException(404, "project not found")
        try:
            deployment_id = store.begin_deploy(project_id)
        except ConflictError:
            raise HTTPException(409, "deployment already running for this project")
        background.add_task(execute, project_id, deployment_id)
        return {"deployment_id": deployment_id, "status": Status.DEPLOYING.value}

    @app.get("/api/projects/{project_id}/deployments")
    def list_deployments(project_id: str):
        if store.get_project(project_id) is None:
            raise HTTPException(404, "project not found")
        return store.list_deployments(project_id)

    @app.get("/api/deployments/{deployment_id}")
    def get_deployment(deployment_id: str):
        deployment = store.get_deployment(deployment_id)
        if deployment is None:
            raise HTTPException(404, "deployment not found")
        return deployment

    @app.get("/api/deployments/{deployment_id}/events")
    async def stream_events(deployment_id: str, request: Request, after: int = 0):
        if store.get_deployment(deployment_id) is None:
            raise HTTPException(404, "deployment not found")
        last = request.headers.get("last-event-id")
        cursor = int(last) if last and last.isdigit() else after

        async def generate():
            nonlocal cursor
            while True:
                for item in store.list_events(deployment_id, cursor):
                    cursor = item["seq"]
                    yield _sse("deploy", item["event"], item["seq"])
                status = store.get_deployment(deployment_id)["status"]
                if Status(status) in TERMINAL and not store.list_events(deployment_id, cursor):
                    yield _sse("end", {"status": status})
                    return
                if await request.is_disconnected():
                    return
                await asyncio.sleep(SSE_POLL_S)

        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/webhooks/github")
    async def github_webhook(request: Request, background: BackgroundTasks):
        secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
        if not secret:
            raise HTTPException(503, "webhook secret is not configured")
        body = await request.body()
        if not webhook.verify_signature(secret, body, request.headers.get("x-hub-signature-256")):
            raise HTTPException(401, "invalid signature")

        event = request.headers.get("x-github-event", "")
        if event == "ping":
            return {"ok": True}
        if event != "push":
            return {"ignored": "event"}
        try:
            payload = webhook.load_payload(body, request.headers.get("content-type", ""))
            if webhook.is_tag_push(payload):
                return {"ignored": "tag push"}
            push = webhook.parse_push_details(payload)
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(400, f"malformed push payload: {exc}")

        if push.deleted:
            return {"ignored": "branch deleted"}

        delivery = request.headers.get("x-github-delivery", "")
        if not delivery or not store.record_delivery(delivery):
            return {"ignored": "duplicate"}
        projects = store.find_projects(push.repo_key, push.branch)
        if not projects:
            return {"ignored": "no matching project"}

        redeploys = []
        for project_id in projects:
            decision = plan_redeploy(push, store.get_analysis(project_id, push.before))
            deployment_id = store.create_push_deployment(project_id, push.after, decision.mode, decision.reasons)
            try:
                started = store.begin_deploy(project_id)
            except ConflictError:
                state = "queued"
            else:
                state = "started"
                background.add_task(execute, project_id, started)
            redeploys.append({"project_id": project_id, "deployment_id": deployment_id, "state": state,
                              **decision.as_dict()})

        return JSONResponse({
            "accepted": True,
            "before": push.before,
            "commit": push.after,
            "changed_files": list(push.changed_files),
            "forced": push.forced,
            "projects": projects,
            "redeploys": redeploys,
        }, status_code=202)

    return app


_app = None


def __getattr__(name):
    """`uvicorn control_plane.app:app`이 접근할 때만 앱을 만든다. import만 해서는 DB를 열지 않는다."""
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)
