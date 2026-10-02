"""분석 단계(구간 2~7) 연결. 조종실은 모듈을 import하지 않고 명령으로 실행한다(schemas/README.md 계약).

- Analyzer(C): `python -m analyzer --repo-map <p> --snapshot <d> [--replay <r>]`
  stdout = Intent JSON, stderr 마지막 줄 = {"metrics": {...}} 또는 {"error": ..., "metrics": {...}}, 실패 시 종료 코드 1
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


class AnalysisFailed(Exception):
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
    """C Analyzer CLI를 실행해 (intent, metrics)를 돌려준다. 실패하면 AnalysisFailed."""
    cmd = [sys.executable, "-m", "analyzer", "--repo-map", str(repo_map_path), "--snapshot", str(snapshot_dir)]
    if replay:
        cmd += ["--replay", str(replay)]
    try:
        proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AnalysisFailed("analyzer_timeout")
    report = _last_json(proc.stderr)
    if proc.returncode != 0:
        raise AnalysisFailed(report.get("error", f"exit_{proc.returncode}"), report.get("metrics"))
    try:
        return json.loads(proc.stdout), report.get("metrics", {})
    except json.JSONDecodeError:
        raise AnalysisFailed("analyzer_bad_output", report.get("metrics"))


def _analyzer_root():
    """INFRAMORPH_ANALYZER_ROOT가 있으면 그곳, 없으면 이 레포에 analyzer 패키지가 있을 때(#22 병합 후) 레포 루트."""
    if root := os.environ.get("INFRAMORPH_ANALYZER_ROOT"):
        return Path(root)
    return REPO_ROOT if (REPO_ROOT / "analyzer" / "__main__.py").exists() else None


def fixture_analyzer(deployment):
    """커밋이 fixture(v1·v2)면 그 repo_map·plan에, 가능하면 실제 C Analyzer의 intent를 붙여 돌려준다.

    - 커밋이 없는 수동 배포는 v1을 스냅샷한 것으로 본다. 모르는 커밋이면 None(분석 없음).
    - Analyzer는 기본적으로 저장된 응답 재생(--replay, 비용 0). INFRAMORPH_ANALYZER_LIVE=1이면 실제 모델 호출.
    - rebuild_only는 호출하지 않는다(Change Detector가 AI 생략을 결정한 경우).
    """
    for version in FIXTURE_VERSIONS:
        folder = FIXTURES / version
        repo_map = json.loads((folder / "repo_map.json").read_text())
        if deployment["commit_sha"] not in (None, repo_map["commit"]):
            continue
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
            result["intent"], result["metrics"] = run_analyzer(
                root, case / "repo_map.json", case / "snapshot", replay)
            result["repo_map"] = json.loads((case / "repo_map.json").read_text())
        return result
    return None
