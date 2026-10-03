"""Control Plane API (구간 1·13·15).

실행: uvicorn control_plane.app:app --reload --port 8000  → http://localhost:8000/docs
"""
import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

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
from .orchestrator import record_verification, run_deployment
from .orchestrator import stream_events as run_module
from .module_commands import build_cmds, fake_deployer_cmd  # noqa: F401 (fake_deployer_cmd: 테스트·대역용)
from .module_commands import deployer_cmd as module_deployer_cmd
from .verify import check_url

DEFAULT_DB = Path(os.environ.get("INFRAMORPH_HOME", "/tmp/inframorph")) / "control_plane.db"
REPO_URL = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?(\.git)?/?")
BRANCH = re.compile(r"[A-Za-z0-9._/-]{1,100}")
SSE_POLL_S = 0.5
WEBHOOK_PATH = "/api/webhooks/github"
WEB_DIST = Path(__file__).resolve().parent / "web" / "dist"


class NoCacheHtml(StaticFiles):
    """index.html은 매번 새로 받게 한다. 이름에 해시가 붙은 assets/ 파일만 브라우저가 캐시한다(새 빌드가 바로 보이게)."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if not path.startswith("assets/"):
            response.headers["Cache-Control"] = "no-cache"
        return response
BUILD_TIMEOUT_S = float(os.environ.get("INFRAMORPH_BUILD_TIMEOUT", "900"))
STAGE_NAMES = {"analyze": "AI 분석", "policy": "판단 검사", "plan": "배포 설계 검사", "patch": "코드 수정"}


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


class DeployIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    demo_version: Literal["v1", "v2"] | None = None
    targets: list[Target] | None = Field(default=None, min_length=1)  # 이번 배포의 대상. 없으면 프로젝트 기본값


class RepoDeployIn(ProjectIn):
    """한 화면에서 레포·브랜치·대상을 고르고 바로 배포. 같은 레포·브랜치면 같은 프로젝트(같은 앱)를 쓴다."""
    demo_version: Literal["v1", "v2"] | None = None


def _sse(event, data, seq=None):
    head = f"id: {seq}\n" if seq is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def create_app(db_path=None, deployer_cmd=module_deployer_cmd, analyzer=fixture_analyzer, patcher=fixture_patcher,
               builder=build_cmds, runtime=None):
    """D module hooks, or an explicit C worker owning patch/gate/build/recovery."""
    store = Store(db_path or DEFAULT_DB)
    store.recover_interrupted()
    if runtime is not None:
        analyzer = lambda deployment, folder: runtime.analyze(store, deployment)
        deployer_cmd = lambda deployment, target, folder: runtime.command(store, deployment["id"], target)
    # 배포별 작업 폴더: <INFRAMORPH_HOME>/<deployment_id>/. C 모듈은 심볼릭 링크가 낀 경로를 거부하므로
    # macOS의 /tmp(→ /private/tmp) 같은 링크를 풀어 실제 경로로 넘긴다.
    workdir = store.path.parent.resolve()

    def execute(project_id, deployment_id):
        """분석 → 승인 → 코드 수정 → 검사·빌드 → 대상별 동시 배포. 끝나면 대기 중인 최신 요청을 이어서 실행한다."""
        while deployment_id:
            deployment = store.get_deployment(deployment_id)
            folder = workdir / deployment_id
            rollback = deployment["triggered_by"] == "rollback"
            if rollback and runtime is None:
                write_plans(deployment, folder)
            if (deployment["approved_at"] or rollback or ready(deployment, folder)) and (
                    rollback or (patched(deployment, folder) and built(deployment, folder))):
                try:
                    cmds = {t: deployer_cmd(deployment, t, folder) for t in deployment["targets"]}
                except StageFailed as exc:
                    stage_event(deployment_id, deployment["targets"], exc.step, "fail", exc.code)
                    store.fail(deployment_id)
                except Exception:
                    store.fail(deployment_id)
                else:
                    run_deployment(store, deployment_id, cmds,
                                   verify=lambda t, url, d=deployment_id: verify(d, t, url))
            elif store.get_deployment(deployment_id)["status"] == Status.AWAITING_APPROVAL.value:
                return  # 승인이 나면 approve가 이어서 실행한다
            deployment_id = None
            if store.has_queued(project_id):
                try:
                    deployment_id = store.begin_deploy(project_id)
                except ConflictError:
                    return

    def stage_event(deployment_id, targets, step, status, detail=None, duration_ms=None):
        """조종실이 직접 진행한 단계(분석·코드 수정 등)도 배포기와 같은 DeployEvent로 대상별 타임라인에 남긴다."""
        for target in targets:
            event = DeployEvent(deployment_id=deployment_id, ts=datetime.now(timezone.utc), target=target, step=step,
                                status=status, detail=detail, duration_ms=duration_ms)
            store.add_event(deployment_id, event.model_dump(mode="json", exclude_none=True))

    def ready(deployment, folder):
        """분석·설계(구간 2~7)를 하고 승인이 필요 없으면 True. 실패면 FAILED, 구조 변경이면 승인 대기."""
        targets = deployment["targets"]
        started = time.monotonic()
        try:
            result = analyzer(deployment, folder)
        except StageFailed as exc:
            stage_event(deployment["id"], targets, exc.step, "fail", f"{STAGE_NAMES[exc.step]} 실패: {exc.code}")
            store.set_analysis_metrics(deployment["id"], {**exc.metrics, "error": exc.code})
            store.fail(deployment["id"])
            return False
        if result is None:  # 분석 결과가 없으면(가짜 모드의 임의 커밋) 비교할 구조도 없다
            return True
        try:
            from .results import checked_payload
            checked_payload(result)
            metrics = result.get("metrics") or {}
            store.set_analysis_metrics(deployment["id"], metrics)
            store.set_commit(deployment["id"], result["commit_sha"])
            store.save_analysis(deployment["project_id"], result["commit_sha"], result["repo_map"], result["intent"])
            store.save_plans(deployment["id"], result["plans"])
            store.save_initial_analysis(deployment["id"], result)
        except (ValueError, KeyError, TypeError):
            store.fail(deployment["id"])
            return False
        detail = ("AI 분석 생략 · 이전 결과 재사용" if deployment["analysis_mode"] == "rebuild_only"
                  else "예시 분석 결과 사용 (Analyzer 미연결)" if metrics.get("backend") == "fixture"
                  else f"로컬 Codex · {metrics.get('model')} · 모델 호출 {metrics.get('model_calls', 0)}회"
                  if metrics.get("backend") == "codex-cli"
                  else f"모델 호출 {metrics.get('model_calls', 0)}회")
        stage_event(deployment["id"], targets, "analyze", "ok", detail, int((time.monotonic() - started) * 1000))
        if result.get("intent_checked"):
            stage_event(deployment["id"], targets, "policy", "ok", "AI 판단 근거 확인 (E Policy Gate)")
        # 대상마다 그 대상의 직전 정상 구조와 비교해 인프라가 바뀌면 사람 승인을 받는다(기획서 시나리오 B-2)
        old = store.last_live_plans(deployment["project_id"], deployment["id"], result["plans"])
        changes = plan_diff(old, result["plans"])
        if runtime is not None:
            # 처음 가는 실제 배포 환경은 무엇이 만들어지는지 사람이 확인한다.
            first = {"aws": "aws: 최초 실제 배포 · 프로젝트 전용 ECS·DB·S3 리소스 생성",
                     "onprem": "onprem: 사내 서버에 처음 배포 · 컨테이너·DB 볼륨 생성"}
            for target, reason in first.items():
                if target in result["plans"] and target not in old:
                    changes = [c for c in changes if not c.startswith(target + ":")] + [reason]
        if changes:
            store.await_approval(deployment["id"], changes)
        return not changes

    def patched(deployment, folder):
        """C worker owns patch/gate/build/retry when an explicit runtime is supplied."""
        if runtime is not None:
            return True
        plans = store.get_plans(deployment["id"])
        targets = list(plans) or deployment["targets"]
        stage_event(deployment["id"], targets, "patch", "started")
        started = time.monotonic()
        try:
            manifests = patcher(deployment, plans, folder)
        except StageFailed as exc:
            stage_event(deployment["id"], targets, "patch", "fail", f"코드 수정 실패: {exc.code}")
            store.fail(deployment["id"])
            return False
        elapsed = int((time.monotonic() - started) * 1000)
        for target in targets:
            manifest = (manifests or {}).get(target)
            detail = (f"파일 {len(manifest['changes'])}개 수정" if manifest and manifest["status"] == "patched"
                      else "수정할 코드 없음" if manifest else "코드 수정 생략 (모듈 또는 이 커밋의 스냅샷 없음)")
            stage_event(deployment["id"], [target], "patch", "ok", detail, elapsed)
        return True

    def built(deployment, folder):
        """검사·빌드(구간 8~9, E Builder). 대상마다 차례로 빌드한다(같은 app:<sha> 태그를 동시에 만들지 않게)."""
        if runtime is not None:
            return True
        try:
            commands = builder(deployment, store.get_plans(deployment["id"]), folder)
        except StageFailed as exc:
            stage_event(deployment["id"], deployment["targets"], "build", "fail", exc.code)
            store.fail(deployment["id"])
            return False
        for target, (cwd, cmd) in commands.items():
            exit_code, events, _ = run_module(store, deployment["id"], target, cmd, BUILD_TIMEOUT_S, cwd)
            if exit_code != 0 or any(e.status == "fail" for e in events):
                if not any(e.status == "fail" for e in events):
                    stage_event(deployment["id"], [target], "build", "fail", f"빌드 실패: exit_{exit_code}")
                store.fail(deployment["id"])
                return False
        return True

    def verify(deployment_id, target, url):
        """배포기가 알려 준 주소를 조종실이 직접 호출한다. 주소에 경로가 없으면 plan의 공개 서비스 health 경로를 붙인다."""
        plan = store.get_plans(deployment_id).get(target) or {}
        health = next((s["health"] for s in plan.get("services", []) if s.get("public") and s.get("health")), "/health")
        return check_url(url, health)

    def write_plans(deployment, folder):
        """롤백 작업에는 복사된 plan만 있으므로 배포기가 읽을 plan 파일을 작업 폴더에 쓴다."""
        folder.mkdir(parents=True, exist_ok=True)
        for target, plan in store.get_plans(deployment["id"]).items():
            (folder / f"plan.{target}.json").write_text(json.dumps(plan))

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

    @app.get("/api/runtime")
    def runtime_info():
        from analyzer.config import REASONING_EFFORT
        backend = getattr(runtime, "analysis_backend", "module")
        return {"analysis_backend": backend,
                "aws_enabled": getattr(runtime, "aws_config", None) is not None,
                "onprem_enabled": getattr(runtime, "onprem_config", None) is not None,
                "model": getattr(runtime, "analysis_model", None) if backend in {"codex-cli", "openai"} else None,
                "reasoning_effort": REASONING_EFFORT if backend in {"codex-cli", "openai"} else None,
                "mapper_mode": getattr(runtime, "mapper_mode", "module"),
                "demo_versions": getattr(runtime, "demo_versions", lambda: [])()}

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
    def deploy(project_id: str, background: BackgroundTasks, body: DeployIn | None = None):
        project = store.get_project(project_id)
        if project is None:
            raise HTTPException(404, "project not found")
        return start_deploy(project, background, body.demo_version if body else None,
                            [t.value for t in body.targets] if body and body.targets else None)

    @app.post("/api/deploy", status_code=202)
    def deploy_repo(body: RepoDeployIn, background: BackgroundTasks):
        repo_key = webhook.normalize_repo_url(body.repo_url)
        found = store.find_projects(repo_key, body.branch)
        targets = [t.value for t in body.targets]
        project_id = found[0] if found else store.create_project(body.repo_url, body.branch, targets, repo_key)["project_id"]
        return start_deploy(store.get_project(project_id), background, body.demo_version, targets) | {"project_id": project_id}

    def start_deploy(project, background, demo_version, targets):
        project_id = project["project_id"]
        revision = None
        if demo_version is not None:
            resolve = getattr(runtime, "demo_revision", None)
            if resolve is None:
                raise HTTPException(409, "demo version selection requires the explicit demo runtime")
            try:
                revision = resolve(project, demo_version)
            except ValueError:
                raise HTTPException(409, "selected demo version is unavailable for this project") from None
        try:
            deployment_id = store.begin_deploy(project_id, revision=revision, targets=targets)
        except ConflictError:
            raise HTTPException(409, "deployment already running for this project")
        background.add_task(execute, project_id, deployment_id)
        return {"deployment_id": deployment_id, "status": Status.DEPLOYING.value}

    @app.get("/api/projects/{project_id}/deployments")
    def list_deployments(project_id: str):
        if store.get_project(project_id) is None:
            raise HTTPException(404, "project not found")
        return store.list_deployments(project_id)

    @app.post("/api/deployments/{deployment_id}/retry", status_code=202)
    def retry(deployment_id: str, background: BackgroundTasks):
        deployment = deployment_or_404(deployment_id)
        if runtime is None:
            raise HTTPException(409, "validated retry requires the explicit runtime")
        try:
            new_id = store.retry_validated_deployment(deployment_id)
        except (ValueError, ConflictError):
            raise HTTPException(409, "validated retry unavailable or project is running")
        background.add_task(execute, deployment["project_id"], new_id)
        return {"deployment_id": new_id, "status": Status.DEPLOYING.value}

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
        saved = store.get_deployment_analysis(deployment_id)
        cached = store.get_analysis(deployment["project_id"], deployment["commit_sha"]) if deployment["commit_sha"] else None
        repo_map = cached["repo_map"] if cached else None  # 화면의 '앱 코드 구조' 트리(B Repo Mapper 출력)
        if saved is not None:
            return {"intent": saved["intent"], "metrics": saved["metrics"], "repo_map": repo_map,
                    "initial_intent": saved["initial"]["intent"], "recovery": saved.get("recovery")}
        return {"intent": cached["intent"] if cached else None, "metrics": deployment["analysis_metrics"], "repo_map": repo_map}

    @app.get("/api/deployments/{deployment_id}/policy")
    def get_policy(deployment_id: str):
        deployment_or_404(deployment_id)
        from .policy_results import read
        return read(store, deployment_id)

    @app.get("/api/deployments/{deployment_id}/patch")
    def get_patch(deployment_id: str):
        """대상별 코드 수정 내역(C Code Patch 결과). 화면의 '코드를 이렇게 고쳤다'에 쓴다."""
        deployment = deployment_or_404(deployment_id)
        if runtime is not None:
            return store.get_runtime_patches(deployment_id)
        folder = workdir / deployment_id / "patched"
        return {t: patch for t in deployment["targets"] if (patch := read_patch(folder / t))}

    @app.post("/api/deployments/{deployment_id}/verify")
    def reverify(deployment_id: str):
        """지금 다시 직접 확인(화면의 '다시 확인' 버튼). 타임라인에는 남기지 않고 결과만 갱신한다."""
        deployment = deployment_or_404(deployment_id)
        for target, state in deployment["targets"].items():
            if state["url"]:
                record_verification(store, deployment_id, target, verify(deployment_id, target, state["url"]))
        return store.get_deployment(deployment_id)["targets"]

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
        # 실제로 배포된(설계도가 있는) 직전 LIVE로 돌아간다. 설계도 없이 건너뛴 배포는 기준점이 아니다.
        base = store.last_live(deployment["project_id"], deployment_id, with_plans=True)
        if base is None:
            raise HTTPException(409, "no earlier LIVE deployment to roll back to")
        new_id = store.create_rollback(deployment_id, base)
        try:
            started = store.begin_deploy(deployment["project_id"], targets=list(store.get_plans(new_id)))
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
        app.mount("/", NoCacheHtml(directory=WEB_DIST, html=True), name="web")
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
