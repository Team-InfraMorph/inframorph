"""분석(구간 2~7)과 코드 수정(구간 7~8) 연결. 조종실은 모듈을 import하지 않고 명령으로 실행한다(schemas/README.md 계약).

- Analyzer(C): `python -m analyzer --repo-map <p> --snapshot <d> [--replay <r>]`
  stdout = Intent JSON, stderr 마지막 줄 = {"metrics": {...}} 또는 {"error": ..., "metrics": {...}}, 실패 시 종료 코드 1
- Code Patch(C): `python -m code_patch --snapshot <d> --repo-map <p> --plan <p> --output-dir <새 경로>`
  stdout = manifest JSON(실패 시 {"error": ...}, 종료 코드 1), 출력 폴더에 source/·patch.diff·manifest.json
- Repo Mapper·Planner(B)는 아직 없어서 schemas/fixtures의 repo_map·plan을 커밋에 맞춰 쓴다.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "schemas" / "fixtures"
FIXTURE_VERSIONS = ("v1", "v2")
ANALYZER_TIMEOUT_S = 180
PATCH_TIMEOUT_S = 60
LOCKFILES = ("package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock")


class StageFailed(Exception):
    """분석·코드 수정 단계 실패. code는 화면과 이력에 그대로 보여 줄 짧은 오류 코드다."""

    def __init__(self, code, metrics=None):
        super().__init__(code)
        self.code = code
        self.metrics = metrics or {}


def _last_json(text):
    for line in reversed(text.strip().splitlines()):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return {}


def run_analyzer(root, repo_map_path, snapshot_dir, replay=None, timeout=ANALYZER_TIMEOUT_S):
    """C Analyzer CLI를 실행해 (intent, metrics)를 돌려준다. 실패하면 StageFailed."""
    cmd = [sys.executable, "-m", "analyzer", "--repo-map", str(repo_map_path), "--snapshot", str(snapshot_dir)]
    if replay:
        cmd += ["--replay", str(replay)]
    try:
        proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise StageFailed("analyzer_timeout")
    report = _last_json(proc.stderr)
    if proc.returncode != 0:
        raise StageFailed(report.get("error", f"exit_{proc.returncode}"), report.get("metrics"))
    try:
        return json.loads(proc.stdout), report.get("metrics", {})
    except json.JSONDecodeError:
        raise StageFailed("analyzer_bad_output", report.get("metrics"))


def run_code_patch(root, snapshot_dir, repo_map_path, plan, out_dir, timeout=PATCH_TIMEOUT_S):
    """C Code Patch CLI로 out_dir(새 경로)에 수정본을 만들고 manifest를 돌려준다. 실패하면 StageFailed."""
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    plan_path = out_dir.parent.parent / f"plan.{plan['target']}.json"
    plan_path.write_text(json.dumps(plan))
    cmd = [sys.executable, "-m", "code_patch", "--snapshot", str(snapshot_dir), "--repo-map", str(repo_map_path),
           "--plan", str(plan_path), "--output-dir", str(out_dir)]
    try:
        proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise StageFailed("patch_timeout")
    report = _last_json(proc.stdout)
    if proc.returncode != 0 or "error" in report:
        raise StageFailed(report.get("error", f"exit_{proc.returncode}"))
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


def _analyzer_root():
    """C 모듈(analyzer·code_patch)이 있는 위치. INFRAMORPH_ANALYZER_ROOT가 있으면 그곳,
    없으면 이 레포에 analyzer 패키지가 있을 때(#22 병합 후) 레포 루트."""
    if root := os.environ.get("INFRAMORPH_ANALYZER_ROOT"):
        return Path(root)
    return REPO_ROOT if (REPO_ROOT / "analyzer" / "__main__.py").exists() else None


def fixture_analyzer(deployment):
    """커밋이 fixture(v1·v2)면 그 repo_map·plan에, 가능하면 실제 C Analyzer의 intent를 붙여 돌려준다.

    - 모르는 커밋이면 None(분석 없음).
    - Analyzer는 기본적으로 저장된 응답 재생(--replay, 비용 0). INFRAMORPH_ANALYZER_LIVE=1이면 실제 모델 호출.
    - rebuild_only는 호출하지 않는다(Change Detector가 AI 생략을 결정한 경우).
    """
    version = _fixture_version(deployment["commit_sha"])
    if version is None:
        return None
    folder = FIXTURES / version
    repo_map = json.loads((folder / "repo_map.json").read_text())
    result = {
        "commit_sha": repo_map["commit"],
        "repo_map": repo_map,
        "intent": json.loads((folder / "intent.json").read_text()),
        "plans": {t: json.loads((folder / f"plan.{t}.json").read_text()) for t in deployment["targets"]},
        "metrics": {"backend": "fixture", "model_calls": 0},
    }
    root = _analyzer_root()
    if root is not None and deployment.get("analysis_mode") != "rebuild_only":
        case = root / "tests" / "fixtures" / "analyzer" / version
        replay = None if os.environ.get("INFRAMORPH_ANALYZER_LIVE") == "1" else case / "replay.json"
        result["intent"], result["metrics"] = run_analyzer(root, case / "repo_map.json", case / "snapshot", replay)
        result["repo_map"] = json.loads((case / "repo_map.json").read_text())
    return result


def fixture_patcher(deployment, plans, workdir):
    """대상별 plan으로 C Code Patch를 돌려 workdir/patched/<target>에 수정본을 만든다.

    C 모듈이나 fixture 스냅샷이 없으면 None(코드 수정 단계 생략). 결과 = {target: manifest}.
    """
    root, version = _analyzer_root(), _fixture_version(deployment["commit_sha"])
    if root is None or not (root / "code_patch" / "__main__.py").exists() or version is None or not plans:
        return None
    case = root / "tests" / "fixtures" / "analyzer" / version
    return {t: run_code_patch(root, case / "snapshot", case / "repo_map.json", plan, workdir / "patched" / t)
            for t, plan in plans.items()}
