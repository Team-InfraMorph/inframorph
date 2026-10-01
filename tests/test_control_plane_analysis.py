import json
import os
import tempfile
import textwrap
import unittest
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=DeprecationWarning)

from fastapi.testclient import TestClient  # noqa: E402

from control_plane.analysis import AnalysisFailed, run_analyzer  # noqa: E402
from control_plane.app import create_app  # noqa: E402

V1 = Path(__file__).resolve().parents[1] / "schemas" / "fixtures" / "v1"
METRICS = {"backend": "replay", "model_calls": 5, "tool_calls": 4, "estimated_usd": 0.0, "duration_ms": 3}


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
            with self.assertRaises(AnalysisFailed) as ctx:
                run_analyzer(root, case / "repo_map.json", case / "snapshot")
        self.assertEqual(ctx.exception.code, "schema_invalid")
        self.assertEqual(ctx.exception.metrics, {"model_calls": 2})


class AnalyzerInPipelineTest(unittest.TestCase):
    def deploy(self, exit_code=0):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as stub:
            os.environ["INFRAMORPH_ANALYZER_ROOT"] = str(make_stub_root(stub, exit_code)[0])
            os.environ["INFRAMORPH_FAKE_DELAY"] = "0"
            try:
                app = create_app(db_path=Path(tmp) / "cp.db")
                with TestClient(app) as client:
                    project = client.post("/api/projects", json={
                        "repo_url": "https://github.com/Team-InfraMorph/demo-app", "targets": ["local", "aws"]}).json()
                    dep = client.post(f"/api/projects/{project['project_id']}/deploy").json()["deployment_id"]
                    return client.get(f"/api/deployments/{dep}").json()
            finally:
                os.environ.pop("INFRAMORPH_ANALYZER_ROOT", None)
                os.environ.pop("INFRAMORPH_FAKE_DELAY", None)
                app.state.store.close()

    def test_analyzer_metrics_are_recorded_for_the_screen(self):
        deployment = self.deploy()
        self.assertEqual(deployment["status"], "LIVE")
        self.assertEqual(deployment["analysis_metrics"]["model_calls"], 5)

    def test_analyzer_failure_stops_before_deploying(self):
        deployment = self.deploy(exit_code=1)
        self.assertEqual(deployment["status"], "FAILED")
        self.assertEqual(deployment["analysis_metrics"]["error"], "schema_invalid")
        self.assertEqual({t["status"] for t in deployment["targets"].values()}, {"FAILED"})


if __name__ == "__main__":
    unittest.main()
