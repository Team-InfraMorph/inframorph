"""Actual C/E gate boundaries; Docker is forbidden in these regression tests."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from analyzer.e_runtime import EConnector, run_worker
from analyzer import e_worker
from code_patch import patch_snapshot
from code_patch.runner import make_diff
from control_plane.app import create_app
from control_plane.b_bridge import DemoModules
from control_plane.db import Store
from control_plane.local_deploy import deploy, load_context
from control_plane.runtime import LocalRuntime
from control_plane.module_commands import build_cmds, deployer_cmd
from control_plane.analysis import StageFailed
from policy_gate.gate import PolicyError, digest, read_tree, sha
from scripts.redteam_boundaries import verify_source
from scripts.verify_e_redteam import verify

ROOT = Path(__file__).resolve().parents[1]


class GateWiringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root / "cp.db")
        self.addCleanup(self.store.close)
        self.runtime = LocalRuntime(root=self.root / "runtime", b_modules=DemoModules(), publish=True)
        project = self.store.create_project("https://github.com/Team-InfraMorph/demo-app", "main", ["local"])
        self.did = self.store.begin_deploy(project["project_id"])
        result = self.runtime.analyze(self.store, self.store.get_deployment(self.did))
        self.store.set_commit(self.did, result["commit_sha"])
        self.store.save_initial_analysis(self.did, result)
        self.context = load_context(self.runtime.context_file(self.did))

    def test_operator_publish_survives_context_and_worker_and_returns_public_url(self):
        self.assertTrue(self.context.publish)
        connector = EConnector(snapshot=self.context.snapshot, repo_map=self.context.repo_map,
            state_root=self.context.state_root, runtime_name="integration-demo", deployment_id=self.did,
            make_plan=AsyncMock(), publish=self.context.publish)
        artifact = {"source_revision": self.context.repo_map.commit, "target": "local",
                    "image": self.context.plan.image_tag, "platform": "linux/amd64"}
        from schemas import BuildArtifact
        with patch("analyzer.e_runtime.run_worker", AsyncMock(return_value={"ok": True,
                   "deployment": {"url": "https://test.trycloudflare.com", "image_id": "sha256:test"}})) as worker:
            asyncio.run(connector.check_local(BuildArtifact.model_validate(artifact), self.context.plan))
        payload = worker.call_args.args[0]
        self.assertTrue(payload["publish"])
        with patch("adapters.local.runtime.deploy", return_value={"url": "http://127.0.0.1:1",
                   "public_url": "https://test.trycloudflare.com", "image_id": "sha256:test"}) as adapter:
            reply = e_worker.perform(payload)
        self.assertTrue(adapter.call_args.kwargs["publish"])
        self.assertEqual(reply["deployment"]["url"], "https://test.trycloudflare.com")
        payload["publish"] = "false"
        with patch("adapters.local.runtime.deploy") as adapter, self.assertRaises(ValueError):
            e_worker.perform(payload)
        adapter.assert_not_called()

    def test_consistent_but_forbidden_patch_stops_before_builder_and_adapter(self):
        def malicious(snapshot, repo_map, plan, output):
            manifest = patch_snapshot(snapshot, repo_map, plan, output)
            file = output / "source/src/storage.js"
            file.write_text(file.read_text() + "\nrequire('child_process');\n")
            before, _ = read_tree(snapshot)
            after, _ = read_tree(output / "source")
            diff, changes = make_diff(before, after)
            (output / "patch.diff").write_text(diff)
            manifest.update(changes=changes, patched_digest=digest(after), diff_sha256=sha(diff.encode()))
            (output / "manifest.json").write_text(json.dumps(manifest))
            return manifest
        observed = []
        reasons = []
        async def record(payload):
            observed.append(payload["action"])
            try:
                return await run_worker(payload)
            except ValueError as error:
                reasons.append(str(error))
                raise
        with patch("control_plane.local_deploy.patch_snapshot", side_effect=malicious), \
             patch("analyzer.e_runtime.run_worker", side_effect=record), \
             patch.object(EConnector, "build", AsyncMock()) as builder, \
             patch.object(EConnector, "check_local", AsyncMock()) as adapter:
            result = asyncio.run(deploy(self.context, self.store))
        self.assertEqual(result, 1)
        self.assertEqual(observed, ["intent", "patch"])
        self.assertEqual(reasons, ["forbidden_code_pattern"])
        builder.assert_not_awaited()
        adapter.assert_not_awaited()
        self.assertEqual(self.store.get_runtime_patches(self.did), {})

    def test_changed_snapshot_stops_before_any_worker(self):
        path = Path(self.context.snapshot) / "src/server.js"
        path.write_text(path.read_text() + "\n// changed after analysis\n")
        with patch("analyzer.e_runtime.run_worker", AsyncMock()) as worker:
            self.assertEqual(asyncio.run(deploy(self.context, self.store)), 1)
        worker.assert_not_awaited()

    def test_missing_inputs_never_use_implicit_fake_deployer(self):
        with patch.dict(os.environ, {"INFRAMORPH_DEMO_MODE": "0", "INFRAMORPH_MODULES_ROOT": str(ROOT)}):
            with self.assertRaises(StageFailed):
                build_cmds({}, {"local": {}}, self.root / "missing")
            with self.assertRaises(StageFailed):
                deployer_cmd({"id": self.did, "triggered_by": "manual"}, "local", self.root / "missing")

    def test_default_api_records_missing_b_as_failure(self):
        with patch.dict(os.environ, {"INFRAMORPH_DEMO_MODE": "0"}):
            app = create_app(db_path=self.root / "missing-b.db")
            try:
                with TestClient(app) as client:
                    project = client.post("/api/projects", json={"repo_url": "https://github.com/o/r", "targets": ["local"]}).json()
                    did = client.post(f"/api/projects/{project['project_id']}/deploy").json()["deployment_id"]
                    self.assertEqual(client.get(f"/api/deployments/{did}").json()["status"], "FAILED")
                    events = app.state.store.list_events(did)
                    self.assertTrue(any("mapper_planner_not_connected" in e["event"].get("detail", "") for e in events))
            finally:
                app.state.store.close()


class CorpusIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = Path(os.environ.get("INFRAMORPH_REDTEAM_CORPUS", ROOT.parent / "redteam-repo"))
        if not (cls.corpus / "fixtures/manifest.json").exists():
            raise unittest.SkipTest("Cross-repo corpus not installed; e-runtime CI supplies the pinned checkout")

    def test_all_cases_execute_real_host_boundaries(self):
        report = verify(self.corpus)
        self.assertEqual((report["passed"], report["failed"], report["not_run"]), (25, 0, 0))
        self.assertEqual(report["live_model_behavior"], "not_measured")

    def test_expectation_tampering_and_missing_case_cannot_turn_green(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "corpus"
            subprocess.run(["git", "clone", "--quiet", "--shared", str(self.corpus), str(target)], check=True)
            path = target / "fixtures/manifest.json"
            original = path.read_text()
            data = json.loads(original)
            data["cases"][0]["expected"]["decision"] = "reject"
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(PolicyError, "corpus_fixture_changed"):
                verify_source(target)
            data = json.loads(original)
            data["cases"].pop()
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(PolicyError, "corpus_fixture_changed"):
                verify_source(target)
