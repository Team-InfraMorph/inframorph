import json
import os
import tempfile
import textwrap
import unittest
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=DeprecationWarning)

from fastapi.testclient import TestClient  # noqa: E402

from control_plane.analysis import StageFailed, run_analyzer  # noqa: E402
from control_plane.app import create_app  # noqa: E402

V1 = Path(__file__).resolve().parents[1] / "schemas" / "fixtures" / "v1"
METRICS = {"backend": "replay", "model_calls": 5, "tool_calls": 4, "estimated_usd": 0.0, "duration_ms": 3}


MANIFEST = {"status": "patched", "changes": [
    {"path": "package-lock.json", "action": "add"}, {"path": "prisma/schema.prisma", "action": "modify"}]}
DIFF = "--- /dev/null\n+++ b/package-lock.json\n+{}\n--- a/prisma/schema.prisma\n+++ b/prisma/schema.prisma\n" \
       "-    provider = \"sqlite\"\n+    provider = \"postgresql\"\n"


def add_code_patch_stub(root, exit_code=0):
    """C Code Patch CLI와 같은 계약: --output-dir에 manifest.json·patch.diff를 만들고 stdout에 manifest."""
    (root / "code_patch").mkdir()
    (root / "code_patch" / "__main__.py").write_text(textwrap.dedent(f"""
        import json, sys
        from pathlib import Path
        if {exit_code}:
            print(json.dumps({{"error": "unsupported_source"}}))
            raise SystemExit({exit_code})
        out = Path(sys.argv[sys.argv.index("--output-dir") + 1])
        if out.resolve() != out:  # 실제 C 모듈처럼 심볼릭 링크가 낀 경로는 거부(macOS /tmp, /var)
            print(json.dumps({{"error": "symlink_path"}}))
            raise SystemExit(1)
        out.mkdir()
        (out / "manifest.json").write_text(json.dumps({MANIFEST!r}))
        (out / "patch.diff").write_text({DIFF!r})
        print(json.dumps({MANIFEST!r}))
    """))


def make_stub_root(tmp, exit_code=0):
    """C Analyzer CLI와 같은 입출력 계약을 가진 가짜 패키지와 v1 케이스 폴더를 만든다."""
    root = Path(tmp)
    (root / "analyzer").mkdir()
    (root / "analyzer" / "__main__.py").write_text(textwrap.dedent(f"""
        import json, sys
        if {exit_code}:
            print(json.dumps({{"error": "schema_invalid", "metrics": {{"model_calls": 2}}}}), file=sys.stderr)
            raise SystemExit({exit_code})
        print(open({str(V1 / "intent.json")!r}).read())
        print("diagnostic line", file=sys.stderr)
        print(json.dumps({{"metrics": {METRICS!r}}}), file=sys.stderr)
    """))
    case = root / "tests" / "fixtures" / "analyzer" / "v1"
    (case / "snapshot").mkdir(parents=True)
    (case / "repo_map.json").write_text((V1 / "repo_map.json").read_text())
    (case / "replay.json").write_text("{}")
    return root, case


class RunAnalyzerTest(unittest.TestCase):
    def test_success_returns_intent_and_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, case = make_stub_root(tmp)
            intent, metrics = run_analyzer(root, case / "repo_map.json", case / "snapshot", case / "replay.json")
        self.assertEqual(intent["source_revision"], json.loads((V1 / "intent.json").read_text())["source_revision"])
        self.assertEqual(metrics["model_calls"], 5)

    def test_failure_carries_error_code_and_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, case = make_stub_root(tmp, exit_code=1)
            with self.assertRaises(StageFailed) as ctx:
                run_analyzer(root, case / "repo_map.json", case / "snapshot")
        self.assertEqual(ctx.exception.code, "schema_invalid")
        self.assertEqual(ctx.exception.metrics, {"model_calls": 2})


class AnalyzerInPipelineTest(unittest.TestCase):
    def deploy(self, exit_code=0, patch_exit_code=None):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as stub:
            root = make_stub_root(stub, exit_code)[0]
            if patch_exit_code is not None:
                add_code_patch_stub(root, patch_exit_code)
            os.environ["INFRAMORPH_MODULES_ROOT"] = str(root)
            os.environ["INFRAMORPH_FAKE_DELAY"] = "0"
            try:
                app = create_app(db_path=Path(tmp) / "cp.db")
                with TestClient(app) as client:
                    project = client.post("/api/projects", json={
                        "repo_url": "https://github.com/Team-InfraMorph/demo-app", "targets": ["local", "aws"]}).json()
                    dep = client.post(f"/api/projects/{project['project_id']}/deploy").json()["deployment_id"]
                    deployment = client.get(f"/api/deployments/{dep}").json()
                    deployment["analysis"] = client.get(f"/api/deployments/{dep}/analysis").json()
                    deployment["patch"] = client.get(f"/api/deployments/{dep}/patch").json()
                    deployment["events"] = [e["event"] for e in app.state.store.list_events(dep)]
                    return deployment
            finally:
                os.environ.pop("INFRAMORPH_MODULES_ROOT", None)
                os.environ.pop("INFRAMORPH_FAKE_DELAY", None)
                app.state.store.close()

    def test_analyzer_metrics_are_recorded_for_the_screen(self):
        deployment = self.deploy()
        self.assertEqual(deployment["status"], "LIVE")
        self.assertEqual(deployment["analysis_metrics"]["model_calls"], 5)
        self.assertEqual(deployment["analysis"]["intent"]["workloads"][0]["name"], "web")

    def test_analyzer_failure_stops_before_deploying(self):
        deployment = self.deploy(exit_code=1)
        self.assertEqual(deployment["status"], "FAILED")
        self.assertEqual(deployment["analysis_metrics"]["error"], "schema_invalid")
        self.assertEqual({t["status"] for t in deployment["targets"].values()}, {"FAILED"})

    def test_code_patch_runs_per_target_before_deployers(self):
        deployment = self.deploy(patch_exit_code=0)
        self.assertEqual(deployment["status"], "LIVE")
        patch_ok = [e for e in deployment["events"] if e["step"] == "patch" and e["status"] == "ok"]
        self.assertEqual({e["target"] for e in patch_ok}, {"local", "aws"})
        self.assertEqual(patch_ok[0]["detail"], "파일 2개 수정")
        files = deployment["patch"]["aws"]["files"]
        self.assertIsNone(files[0]["diff"])  # lockfile은 길어서 생략
        self.assertIn('+    provider = "postgresql"', files[1]["diff"])

    def test_code_patch_failure_stops_before_deployers(self):
        deployment = self.deploy(patch_exit_code=1)
        self.assertEqual(deployment["status"], "FAILED")
        self.assertEqual({e["step"] for e in deployment["events"]}, {"analyze", "patch"})  # 배포기는 안 돌았다
        self.assertIn("unsupported_source", deployment["events"][-1]["detail"])


if __name__ == "__main__":
    unittest.main()
