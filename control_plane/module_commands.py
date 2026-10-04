"""빌드·배포 단계(구간 8~12)의 명령. 조종실은 이 명령을 실행해 stdout의 DeployEvent JSONL을 받는다.

| 단계 | 담당 | 명령 |
|---|---|---|
| 검사·빌드 | E Builder | `python -m builder --snapshot --bundle --plan --output --deployment-id` (BuildArtifact를 --output에) |
| Local 배포 | E Local Adapter | `python -m adapters.local deploy --plan --artifact --state-dir --deployment-id [--publish]` |
| Local 롤백 | E Local Adapter | `python -m adapters.local rollback --state-dir --deployment-id` |
| AWS 배포 | A AWS Adapter | `python -m adapters.aws deploy --plan --artifact --state-dir --execute --deployment-id` |
| AWS 롤백 | A AWS Adapter | `python -m adapters.aws rollback --state-dir --execute --deployment-id` |
| GCP 배포 | A GCP Adapter | `python -m adapters.gcp deploy --plan --artifact --state-dir --execute --deployment-id` |
| GCP 롤백 | A GCP Adapter | `python -m adapters.gcp rollback --state-dir --execute --deployment-id` |

빌드는 실제 배포기가 있는 대상만 한다(가짜 배포기는 이미지가 필요 없다). 배포 작업 폴더 구조:
<INFRAMORPH_HOME>/<deployment_id>/{snapshot, patched/<target>, plan.<target>.json, build.<target>.json}
E Local Adapter의 상태 폴더는 앱마다 하나: <INFRAMORPH_HOME>/state/<plan.app> (E 계약: 폴더 이름 = plan.app).
"""
import json
import os
import sys

from .analysis import module_root, StageFailed

ADAPTERS = {"local": "adapters/local", "aws": "adapters/aws", "gcp": "adapters/gcp"}
# AWS Adapter는 실제 계정을 바꾸므로 --execute 없이는 거부한다. 이 명령은 조종실 승인을 거친 뒤에만 실행된다.
EXECUTE = {"aws": ["--execute"], "gcp": ["--execute"]}
NO_BUILD_NOTE = "새 커밋의 코드가 없어 다시 빌드하지 않음 (B Repo Mapper 연결 전) · 기존 버전 유지"


def _fake_env(name, target, default=None):
    """INFRAMORPH_FAKE_<NAME>_<TARGET>이 있으면 그 대상에만, 없으면 INFRAMORPH_FAKE_<NAME>을 쓴다."""
    return os.environ.get(f"INFRAMORPH_FAKE_{name}_{target.upper()}", os.environ.get(f"INFRAMORPH_FAKE_{name}", default))


def fake_deployer_cmd(deployment, target, folder=None):
    """실제 배포기가 없는 대상에 쓰는 명령. 환경 변수로 지연·fixture·종료 코드를 대상별로 바꿀 수 있다."""
    if os.environ.get("INFRAMORPH_DEMO_MODE") != "1":
        raise StageFailed("deployment_inputs_or_adapter_missing", step="start")
    cmd = [sys.executable, "-m", "control_plane.fake_deployer", "--deployment-id", deployment["id"],
           "--target", target,
           "--delay", _fake_env("DELAY", target, "1"),
           "--exit-code", _fake_env("EXIT_CODE", target, "0")]
    if fixture := _fake_env("FIXTURE", target):
        cmd += ["--fixture", fixture]
    return cmd


def _adapter_root(target):
    package = ADAPTERS.get(target)
    return module_root(package) if package else None


def build_cmds(deployment, plans, folder):
    """실제 배포기가 있는 대상만 E Builder 명령을 만든다. {target: (작업 위치, 명령)}. 빌드할 게 없으면 빈 dict."""
    root = module_root("builder")
    if root is None or not (folder / "snapshot").exists():
        if os.environ.get("INFRAMORPH_DEMO_MODE") != "1":
            raise StageFailed("builder_or_snapshot_missing", step="build")
        return {}
    cmds = {}
    for target in plans:
        bundle, plan_path = folder / "patched" / target, folder / f"plan.{target}.json"
        if _adapter_root(target) is None or not bundle.exists() or not plan_path.exists():
            if os.environ.get("INFRAMORPH_DEMO_MODE") != "1":
                raise StageFailed("build_inputs_or_adapter_missing", step="build")
            continue
        cmds[target] = (root, [sys.executable, "-m", "builder", "--snapshot", str(folder / "snapshot"),
                               "--bundle", str(bundle), "--plan", str(plan_path),
                               "--output", str(folder / f"build.{target}.json"),
                               "--deployment-id", deployment["id"]])
    return cmds


def deployer_cmd(deployment, target, folder):
    """대상별 배포 명령. 실제 배포기와 그 입력(plan·빌드 결과)이 있으면 실제, 아니면 가짜 배포기. (작업 위치, 명령)"""
    root = _adapter_root(target)
    plan_path = folder / f"plan.{target}.json"
    if root is not None and plan_path.exists():
        state = folder.parent / "state" / json.loads(plan_path.read_text())["app"]
        base = [sys.executable, "-m", ADAPTERS[target].replace("/", "."), "--deployment-id", deployment["id"]]
        execute = EXECUTE.get(target, [])
        if deployment["triggered_by"] == "rollback":
            return root, base[:3] + ["rollback", "--state-dir", str(state)] + execute + base[3:]
        artifact = folder / f"build.{target}.json"
        if artifact.exists():
            publish = ["--publish"] if target == "local" and os.environ.get("INFRAMORPH_LOCAL_PUBLISH") == "1" else []
            return root, base[:3] + ["deploy", "--plan", str(plan_path), "--artifact", str(artifact),
                                     "--state-dir", str(state)] + publish + execute + base[3:]
    if root is not None:  # 실제 배포기는 있는데 넘길 빌드 결과가 없다 = 이 커밋의 코드가 없다. 가짜 성공을 보이지 않는다.
        return None, fake_deployer_cmd(deployment, target, folder) + ["--note", NO_BUILD_NOTE]
    return None, fake_deployer_cmd(deployment, target, folder)
