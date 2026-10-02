import json
import os
import tempfile
import textwrap
import unittest
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=DeprecationWarning)

from fastapi.testclient import TestClient  # noqa: E402

from control_plane.app import create_app  # noqa: E402
from tests.test_control_plane_analysis import add_code_patch_stub, make_stub_root  # noqa: E402

EVENT = ('{"deployment_id": dep, "ts": "2026-10-02T00:00:00Z", "target": target, "step": step, "status": status}')


def add_e_stubs(root, gate_ok=True, build_ok=True):
    """E 모듈 세 개와 같은 명령·출력 계약. 실제 E처럼 상태 폴더 이름 != plan.app이면 거부한다."""
    helper = textwrap.dedent(f"""
        import json, sys
        from pathlib import Path
        def arg(name):
            return sys.argv[sys.argv.index(name) + 1]
        def emit(dep, target, step, status, **extra):
            print(json.dumps({{**{EVENT}, **extra}}), flush=True)
    """)
    (root / "policy_gate").mkdir()
    (root / "policy_gate" / "__main__.py").write_text(helper + textwrap.dedent(f"""
        assert sys.argv[1] == "intent" and Path(arg("--input")).exists() and arg("--revision")
        if {gate_ok}:
            print(json.dumps({{"status": "ok", "stage": "intent"}}))
        else:
            print(json.dumps({{"status": "fail", "code": "evidence_file_missing"}}))
            raise SystemExit(1)
    """))
    (root / "builder").mkdir()
    (root / "builder" / "__main__.py").write_text(helper + textwrap.dedent(f"""
        plan = json.loads(Path(arg("--plan")).read_text())
        dep, target = arg("--deployment-id"), plan["target"]
        assert Path(arg("--bundle")).exists() and Path(arg("--snapshot")).exists()
        emit(dep, target, "policy", "ok")
        if not {build_ok}:
            emit(dep, target, "build", "fail", detail="image_tag_collision")
            raise SystemExit(1)
        emit(dep, target, "build", "ok")
        Path(arg("--output")).write_text(json.dumps({{"image": plan["image_tag"], "target": target}}))
    """))
    (root / "adapters" / "local").mkdir(parents=True)
    (root / "adapters" / "__init__.py").write_text("")
    (root / "adapters" / "local" / "__main__.py").write_text(helper + textwrap.dedent("""
        dep, state = arg("--deployment-id"), Path(arg("--state-dir"))
        if sys.argv[1] == "rollback":
            emit(dep, "local", "rollback", "ok", url="http://127.0.0.1:49999/health")
            raise SystemExit(0)
        plan = json.loads(Path(arg("--plan")).read_text())
        if state.name != plan["app"]:
            emit(dep, "local", "start", "fail", detail="state_app_mismatch")
            raise SystemExit(1)
        assert json.loads(Path(arg("--artifact")).read_text())["target"] == "local"
        emit(dep, "local", "start", "ok")
        emit(dep, "local", "url", "ok", url="http://127.0.0.1:49999/health")
    """))


class TeamModulesPipelineTest(unittest.TestCase):
    def run_pipeline(self, gate_ok=True, build_ok=True, then_rollback=False):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as stub:
            root = make_stub_root(stub)[0]
            add_code_patch_stub(root)
            add_e_stubs(root, gate_ok, build_ok)
            os.environ["INFRAMORPH_MODULES_ROOT"] = str(root)
            os.environ["INFRAMORPH_FAKE_DELAY"] = "0"
            try:
                app = create_app(db_path=Path(tmp) / "cp.db")
                store = app.state.store
                with TestClient(app) as client:
                    project = client.post("/api/projects", json={
                        "repo_url": "https://github.com/Team-InfraMorph/demo-app", "targets": ["local", "aws"]}).json()
                    dep = client.post(f"/api/projects/{project['project_id']}/deploy").json()["deployment_id"]
                    if then_rollback:
                        second = client.post(f"/api/projects/{project['project_id']}/deploy").json()["deployment_id"]
                        dep = client.post(f"/api/deployments/{second}/rollback").json()["deployment_id"]
                    deployment = client.get(f"/api/deployments/{dep}").json()
                    deployment["events"] = [e["event"] for e in store.list_events(dep)]
                    return deployment
            finally:
                os.environ.pop("INFRAMORPH_MODULES_ROOT", None)
                os.environ.pop("INFRAMORPH_FAKE_DELAY", None)
                store.close()

    def steps(self, deployment, target):
        return [(e["step"], e["status"]) for e in deployment["events"] if e["target"] == target]

    def test_local_runs_real_modules_and_aws_uses_fake_deployer(self):
        deployment = self.run_pipeline()
        self.assertEqual(deployment["status"], "LIVE")
        self.assertEqual(deployment["targets"]["local"]["url"], "http://127.0.0.1:49999/health")
        local = self.steps(deployment, "local")
        for step in [("analyze", "ok"), ("policy", "ok"), ("patch", "ok"), ("build", "ok"), ("url", "ok")]:
            self.assertIn(step, local)
        self.assertNotIn(("build", "ok"), self.steps(deployment, "aws"))  # 가짜 배포기 대상은 빌드하지 않는다
        self.assertEqual(deployment["targets"]["aws"]["url"], "https://example.invalid")

    def test_intent_gate_rejection_stops_before_code_patch(self):
        deployment = self.run_pipeline(gate_ok=False)
        self.assertEqual(deployment["status"], "FAILED")
        self.assertIn(("policy", "fail"), self.steps(deployment, "local"))
        self.assertNotIn("patch", {e["step"] for e in deployment["events"]})

    def test_build_failure_stops_before_deployers(self):
        deployment = self.run_pipeline(build_ok=False)
        self.assertEqual(deployment["status"], "FAILED")
        self.assertIn(("build", "fail"), self.steps(deployment, "local"))
        self.assertNotIn("url", {e["step"] for e in deployment["events"]})

    def test_rollback_uses_local_adapter_rollback(self):
        deployment = self.run_pipeline(then_rollback=True)
        self.assertEqual(deployment["triggered_by"], "rollback")
        self.assertIn(("rollback", "ok"), self.steps(deployment, "local"))
        self.assertNotIn("build", {e["step"] for e in deployment["events"]})


if __name__ == "__main__":
    unittest.main()
