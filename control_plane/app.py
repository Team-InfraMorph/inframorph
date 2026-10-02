"""Control Plane API (구간 1·13·15).

실행: uvicorn control_plane.app:app --reload --port 8000  → http://localhost:8000/docs
"""
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from schemas.common import Target
from schemas.events import DeployEvent

from . import webhook
from .analysis import StageFailed, fixture_analyzer, fixture_patcher, read_patch
from .db import TERMINAL, ConflictError, Status, Store
from .change_detector import plan_diff, plan_redeploy
from .orchestrator import run_deployment

DEFAULT_DB = Path(os.environ.get("INFRAMORPH_HOME", "/tmp/inframorph")) / "control_plane.db"
REPO_URL = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?(\.git)?/?")
BRANCH = re.compile(r"[A-Za-z0-9._/-]{1,100}")
SSE_POLL_S = 0.5
WEBHOOK_PATH = "/api/webhooks/github"
WEB_DIST = Path(__file__).resolve().parent / "web" / "dist"


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


def create_app(db_path=None, deployer_cmd=fake_deployer_cmd, analyzer=fixture_analyzer, patcher=fixture_patcher):
    store = Store(db_path or DEFAULT_DB)
    store.recover_interrupted()
    # 배포별 작업 폴더: <INFRAMORPH_HOME>/<deployment_id>/. C 모듈은 심볼릭 링크가 낀 경로를 거부하므로
    # macOS의 /tmp(→ /private/tmp) 같은 링크를 풀어 실제 경로로 넘긴다.
    workdir = store.path.parent.resolve()

    def execute(project_id, deployment_id):
        """분석·승인 확인 후 대상별 배포기를 동시에 돌리고, 끝나면 대기 중인 최신 요청을 이어서 실행한다."""
        while deployment_id:
            deployment = store.get_deployment(deployment_id)
            rollback = deployment["triggered_by"] == "rollback"  # 이전 이미지를 다시 띄우므로 수정·빌드 생략
            if (deployment["approved_at"] or rollback or ready(deployment)) and (rollback or patched(deployment)):
                targets = deployment["targets"]
                run_deployment(store, deployment_id, {t: deployer_cmd(deployment_id, t) for t in targets})
            elif store.get_deployment(deployment_id)["status"] == Status.AWAITING_APPROVAL.value:
                return  # 승인이 나면 approve가 이어서 실행한다
            deployment_id = None
            if store.has_queued(project_id):
                try:
                    deployment_id = store.begin_deploy(project_id)
                except ConflictError:
                    return

    def ready(deployment):
        """분석·설계(구간 2~7)를 하고 승인이 필요 없으면 True. 분석 실패면 FAILED, 구조 변경이면 승인 대기."""
        try:
            result = analyzer(deployment)
        except StageFailed as exc:
            store.set_analysis_metrics(deployment["id"], {**exc.metrics, "error": exc.code})
            store.fail(deployment["id"])
            return False
        if result is None:  # 분석 결과가 없으면(가짜 모드의 임의 커밋) 비교할 구조도 없다
            return True
        store.set_analysis_metrics(deployment["id"], result.get("metrics"))
        store.set_commit(deployment["id"], result["commit_sha"])
        store.save_analysis(deployment["project_id"], result["commit_sha"], result["repo_map"], result["intent"])
        store.save_plans(deployment["id"], result["plans"])
        # 직전 LIVE 대비 인프라 구조가 바뀌면 사람 승인을 받는다(기획서 시나리오 B-2)
        base = store.last_live(deployment["project_id"], deployment["id"], with_plans=True)
        changes = plan_diff(store.get_plans(base["id"]) if base else {}, result["plans"])
        if changes:
            store.await_approval(deployment["id"], changes)
        return not changes

    def stage_event(deployment_id, target, step, status, detail=None, duration_ms=None):
        """조종실이 직접 진행한 단계(코드 수정 등)도 배포기와 같은 DeployEvent로 타임라인에 남긴다."""
        event = DeployEvent(deployment_id=deployment_id, ts=datetime.now(timezone.utc), target=target, step=step,
                            status=status, detail=detail, duration_ms=duration_ms)
        store.add_event(deployment_id, event.model_dump(mode="json", exclude_none=True))

    def patched(deployment):
        """코드 수정(구간 7~8, C Code Patch). 성공하면 True. E의 Policy Gate·Builder는 이 결과를 받아 이어진다."""
        plans = store.get_plans(deployment["id"])
        targets = list(plans) or deployment["targets"]
        for target in targets:
            stage_event(deployment["id"], target, "patch", "started")
        started = time.monotonic()
        try:
            manifests = patcher(deployment, plans, workdir / deployment["id"])
        except StageFailed as exc:
            for target in targets:
                stage_event(deployment["id"], target, "patch", "fail", f"코드 수정 실패: {exc.code}")
            store.fail(deployment["id"])
            return False
        elapsed = int((time.monotonic() - started) * 1000)
        for target in targets:
            manifest = (manifests or {}).get(target)
            detail = (f"파일 {len(manifest['changes'])}개 수정" if manifest and manifest["status"] == "patched"
                      else "수정할 코드 없음" if manifest else "코드 수정 모듈 없음 · 원본 그대로")
            stage_event(deployment["id"], target, "patch", "ok", detail, elapsed)
        return True

    def deployment_or_404(deployment_id):
        deployment = store.get_deployment(deployment_id)
        if deployment is None:
            raise HTTPException(404, "deployment not found")
        return deployment

    app = FastAPI(title="InfraMorph Control Plane")
    app.state.store = store

    @app.middleware("http")
    async def tunnel_only_webhook(request: Request, call_next):
        """터널(Cloudflare)로 들어온 요청은 서명 검증이 있는 webhook만 허용한다.

        조종실 API·화면에는 로그인이 없으므로, 공개 주소로 배포·롤백을 누를 수 없게 막는다.
        Cloudflare는 자신을 거친 모든 요청에 Cf-Ray 헤더를 붙인다. 노트북 안(localhost)에서는 그대로 쓴다.
        """
        if "cf-ray" in request.headers and request.url.path != WEBHOOK_PATH:
            return JSONResponse({"detail": "only the GitHub webhook is reachable through the tunnel"}, status_code=403)
        return await call_next(request)
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

    @app.get("/api/projects")
    def list_projects():
        return store.list_projects()

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

    @app.get("/api/deployments/{deployment_id}/plans")
    def get_plans(deployment_id: str):
        deployment_or_404(deployment_id)
        return store.get_plans(deployment_id)

    @app.get("/api/deployments/{deployment_id}/analysis")
    def get_analysis(deployment_id: str):
        """이 배포 커밋의 분석 결과(C의 intent). 화면의 'AI가 이해한 앱'에 쓴다."""
        deployment = deployment_or_404(deployment_id)
        cached = store.get_analysis(deployment["project_id"], deployment["commit_sha"]) if deployment["commit_sha"] else None
        return {"intent": cached["intent"] if cached else None, "metrics": deployment["analysis_metrics"]}

    @app.get("/api/deployments/{deployment_id}/patch")
    def get_patch(deployment_id: str):
        """대상별 코드 수정 내역(C Code Patch 결과). 화면의 '코드를 이렇게 고쳤다'에 쓴다."""
        deployment = deployment_or_404(deployment_id)
        folder = workdir / deployment_id / "patched"
        return {t: patch for t in deployment["targets"] if (patch := read_patch(folder / t))}

    @app.post("/api/deployments/{deployment_id}/approve", status_code=202)
    def approve(deployment_id: str, background: BackgroundTasks):
        deployment = deployment_or_404(deployment_id)
        if not store.resolve_approval(deployment_id, approved=True):
            raise HTTPException(409, "deployment is not awaiting approval")
        background.add_task(execute, deployment["project_id"], deployment_id)
        return {"deployment_id": deployment_id, "status": Status.DEPLOYING.value}

    @app.post("/api/deployments/{deployment_id}/reject")
    def reject(deployment_id: str):
        deployment_or_404(deployment_id)
        if not store.resolve_approval(deployment_id, approved=False):
            raise HTTPException(409, "deployment is not awaiting approval")
        return {"deployment_id": deployment_id, "status": Status.FAILED.value}

    @app.post("/api/deployments/{deployment_id}/rollback", status_code=202)
    def rollback(deployment_id: str, background: BackgroundTasks):
        """직전 LIVE 배포의 커밋과 plan으로 다시 배포한다(Local은 이전 app:<sha>, AWS는 이전 task definition)."""
        deployment = deployment_or_404(deployment_id)
        if Status(deployment["status"]) not in TERMINAL:
            raise HTTPException(409, "deployment is still in progress")
        base = store.last_live(deployment["project_id"], deployment_id)
        if base is None:
            raise HTTPException(409, "no earlier LIVE deployment to roll back to")
        new_id = store.create_rollback(deployment_id, base)
        try:
            started = store.begin_deploy(deployment["project_id"])
        except ConflictError:
            return {"deployment_id": new_id, "state": "queued", "commit": base["commit_sha"]}
        background.add_task(execute, deployment["project_id"], started)
        return {"deployment_id": new_id, "state": "started", "commit": base["commit_sha"]}

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

    @app.post(WEBHOOK_PATH)
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

    if WEB_DIST.is_dir():  # npm run build 결과가 있으면 API와 같은 주소에서 화면을 낸다
        app.mount("/", StaticFiles(directory=WEB_DIST, html=True), name="web")
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
