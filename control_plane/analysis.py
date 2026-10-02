"""배포 전 단계(구간 2~8)의 팀원 모듈 연결. 조종실은 모듈을 import하지 않고 명령으로 실행한다(schemas/README.md 계약).

| 단계 | 담당 | 명령 | 결과 |
|---|---|---|---|
| 스냅샷·지도 | B Repo Mapper | 아직 없음 → C의 fixture 스냅샷과 schemas/fixtures repo_map으로 대신 | snapshot/, repo_map.json |
| 분석 | C Analyzer | `python -m analyzer --repo-map --snapshot [--replay]` | stdout intent, stderr 마지막 줄 metrics |
| 판단 검사 | E Policy Gate | `python -m policy_gate intent --snapshot --input --revision` | stdout {"status": "ok"} 또는 {"status": "fail", "code"} |
| 설계 | B Planner | 아직 없음 → schemas/fixtures plan으로 대신 | plan.<target>.json |
| 코드 수정 | C Code Patch | `python -m code_patch --snapshot --repo-map --plan --output-dir` | stdout manifest, 출력 폴더에 source/·patch.diff |

모듈 위치: INFRAMORPH_MODULES_ROOT(팀원 브랜치를 합친 checkout), 없으면 이 레포에 해당 패키지가 있을 때(PR 병합 후) 레포 루트.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "schemas" / "fixtures"
FIXTURE_VERSIONS = ("v1", "v2")
ANALYZER_TIMEOUT_S = 180
STEP_TIMEOUT_S = 60
LOCKFILES = ("package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock")


class StageFailed(Exception):
    """배포 전 단계 실패. step은 타임라인 단계 이름, code는 화면과 이력에 그대로 보여 줄 짧은 오류 코드다."""

    def __init__(self, code, metrics=None, step="analyze"):
        super().__init__(code)
        self.code = code
        self.metrics = metrics or {}
        self.step = step


def module_root(package):
    """팀원 모듈 패키지(예: "analyzer", "adapters/local")가 있는 checkout. 없으면 None(그 단계는 대역 사용)."""
    root = Path(os.environ["INFRAMORPH_MODULES_ROOT"]) if os.environ.get("INFRAMORPH_MODULES_ROOT") else REPO_ROOT
    return root.resolve() if (root / package / "__main__.py").exists() else None


# Preserve C's previously exported analysis exception while D adds patch stages.
AnalysisFailed = StageFailed


def _last_json(text):
    """출력 전체가 JSON이면(여러 줄 들여쓰기 포함) 그대로, 아니면 마지막 JSON 줄(로그 뒤에 결과를 찍는 모듈)."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for line in reversed(text.strip().splitlines()):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return {}


def _run(root, args, timeout, timeout_code, step):
    try:
        return subprocess.run([sys.executable, "-m", *args], cwd=root, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise StageFailed(timeout_code, step=step)


def run_analyzer(root, repo_map_path, snapshot_dir, replay=None, timeout=ANALYZER_TIMEOUT_S):
    """C Analyzer CLI를 실행해 (intent, metrics)를 돌려준다. 실패하면 StageFailed."""
    args = ["analyzer", "--repo-map", str(repo_map_path), "--snapshot", str(snapshot_dir)]
    if replay:
        args += ["--replay", str(replay)]
    proc = _run(root, args, timeout, "analyzer_timeout", "analyze")
    report = _last_json(proc.stderr)
    if proc.returncode != 0:
        raise StageFailed(report.get("error", f"exit_{proc.returncode}"), report.get("metrics"))
    try:
        return json.loads(proc.stdout), report.get("metrics", {})
    except json.JSONDecodeError:
        raise StageFailed("analyzer_bad_output", report.get("metrics"))


def run_intent_gate(root, snapshot_dir, intent, folder, timeout=STEP_TIMEOUT_S):
    """E Policy Gate로 AI 판단(intent)의 근거 파일·줄이 실제 스냅샷에 있는지 검사한다. 실패하면 StageFailed."""
    intent_path = folder / "intent.json"
    intent_path.write_text(json.dumps(intent))
    args = ["policy_gate", "intent", "--snapshot", str(snapshot_dir), "--input", str(intent_path),
            "--revision", intent["source_revision"]]
    proc = _run(root, args, timeout, "intent_gate_timeout", "policy")
    report = _last_json(proc.stdout)
    if proc.returncode != 0 or report.get("status") != "ok":
        raise StageFailed(report.get("code", f"exit_{proc.returncode}"), step="policy")


def run_code_patch(root, snapshot_dir, repo_map_path, plan, out_dir, timeout=STEP_TIMEOUT_S):
    """C Code Patch CLI로 out_dir(새 경로)에 수정본을 만들고 manifest를 돌려준다. 실패하면 StageFailed."""
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    plan_path = out_dir.parent.parent / f"plan.{plan['target']}.json"
    plan_path.write_text(json.dumps(plan))
    args = ["code_patch", "--snapshot", str(snapshot_dir), "--repo-map", str(repo_map_path),
            "--plan", str(plan_path), "--output-dir", str(out_dir)]
    proc = _run(root, args, timeout, "patch_timeout", "patch")
    report = _last_json(proc.stdout)
    if proc.returncode != 0 or "error" in report:
        raise StageFailed(report.get("error", f"exit_{proc.returncode}"), step="patch")
    return report


def read_patch(out_dir):
    """화면용 수정 내역: manifest의 변경 목록과 파일별 diff. lockfile은 길어서 diff를 생략한다."""
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    manifest = json.loads(manifest_path.read_text())
    sections, current = {}, None
    for line in (out_dir / "patch.diff").read_text().splitlines():
        if line.startswith("+++ "):
            current = line[4:].removeprefix("b/")
            sections[current] = []
        elif current is not None and not line.startswith("--- "):
            sections[current].append(line)
    files = [{"path": c["path"], "action": c["action"],
              "diff": None if c["path"].endswith(LOCKFILES) else "\n".join(sections.get(c["path"], []))}
             for c in manifest["changes"]]
    return {"status": manifest["status"], "files": files}


def _fixture_version(commit_sha):
    """커밋에 맞는 fixture 버전(v1·v2). 커밋이 없는 수동 배포는 v1을 스냅샷한 것으로 본다."""
    for version in FIXTURE_VERSIONS:
        repo_map = json.loads((FIXTURES / version / "repo_map.json").read_text())
        if commit_sha in (None, repo_map["commit"]):
            return version
    return None


def prepare_snapshot(c_root, version, folder):
    """B Repo Mapper 대역: C의 fixture 스냅샷을 배포 작업 폴더로 고정하고 승인된 lockfile을 넣는다.

    E Builder는 승인된 lockfile이 있는 원본만 빌드하므로 E 검증 스크립트와 같은 방식으로 준비한다.
    이미 있으면(승인 후 재실행) 그대로 쓴다. 반환 = (snapshot 경로, repo_map 경로).
    """
    snapshot, repo_map_path = folder / "snapshot", folder / "repo_map.json"
    if not snapshot.exists():
        case = c_root / "tests" / "fixtures" / "analyzer" / version
        shutil.copytree(case / "snapshot", snapshot)
        repo_map = json.loads((case / "repo_map.json").read_text())
        lockfile = c_root / "code_patch" / "templates" / "base-package-lock.json"
        if lockfile.exists():
            shutil.copyfile(lockfile, snapshot / "package-lock.json")
            repo_map["tree"] = sorted(set(repo_map["tree"]) | {"package-lock.json"})
        repo_map_path.write_text(json.dumps(repo_map))
    return snapshot, repo_map_path


def fixture_analyzer(deployment, folder):
    """커밋이 fixture(v1·v2)면 분석 결과를 돌려준다. 모르는 커밋이면 None(분석 없음).

    - repo_map·plan은 B 대역(schemas/fixtures). C 모듈이 있으면 스냅샷을 작업 폴더에 고정하고
      intent는 실제 C Analyzer로 만든다(기본 replay = 비용 0, INFRAMORPH_ANALYZER_LIVE=1이면 실제 모델 호출).
    - rebuild_only는 Analyzer를 부르지 않는다(Change Detector가 AI 생략을 결정한 경우).
    - E Policy Gate가 있으면 intent의 근거가 스냅샷에 실제로 있는지 검사한다(intent_checked).
    """
    version = _fixture_version(deployment["commit_sha"])
    if version is None:
        return None
    fixture = FIXTURES / version
    repo_map = json.loads((fixture / "repo_map.json").read_text())
    result = {
        "commit_sha": repo_map["commit"],
        "repo_map": repo_map,
        "intent": json.loads((fixture / "intent.json").read_text()),
        "plans": {t: json.loads((fixture / f"plan.{t}.json").read_text()) for t in deployment["targets"]},
        "metrics": {"backend": "fixture", "model_calls": 0},
        "intent_checked": False,
    }
    c_root = module_root("analyzer")
    if c_root is None:
        return result
    folder.mkdir(parents=True, exist_ok=True)
    snapshot, _ = prepare_snapshot(c_root, version, folder)
    if deployment.get("analysis_mode") != "rebuild_only":
        case = c_root / "tests" / "fixtures" / "analyzer" / version
        replay = None if os.environ.get("INFRAMORPH_ANALYZER_LIVE") == "1" else case / "replay.json"
        result["intent"], result["metrics"] = run_analyzer(c_root, case / "repo_map.json", case / "snapshot", replay)
        result["repo_map"] = json.loads((case / "repo_map.json").read_text())
    if (gate_root := module_root("policy_gate")) is not None:
        run_intent_gate(gate_root, snapshot, result["intent"], folder)
        result["intent_checked"] = True
    return result


def fixture_patcher(deployment, plans, folder):
    """대상별 plan으로 C Code Patch를 돌려 folder/patched/<target>에 수정본을 만든다.

    C 모듈이나 준비된 스냅샷이 없으면 None(코드 수정 단계 생략). 결과 = {target: manifest}.
    """
    root = module_root("code_patch")
    snapshot, repo_map_path = folder / "snapshot", folder / "repo_map.json"
    if root is None or not plans or not snapshot.exists():
        return None
    return {t: run_code_patch(root, snapshot, repo_map_path, plan, folder / "patched" / t)
            for t, plan in plans.items()}
