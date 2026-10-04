"""Control Plane -> deployer command lines (no FastAPI needed). Cloud targets use real adapter contracts."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from tests.cp_isolation import NO_MODULES  # noqa: F401  (keeps the default "no team modules" root)
from control_plane.runtime import deployer_cmd

SHA = "f67109815d689ae52c2474dcb1bf04209d154a5d"


class DeployerCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "modules"
        for package in ("adapters/aws", "adapters/gcp", "adapters/local"):
            (self.root / package).mkdir(parents=True)
            (self.root / package / "__main__.py").write_text("")
        self.previous_root = os.environ.get("INFRAMORPH_MODULES_ROOT")
        os.environ["INFRAMORPH_MODULES_ROOT"] = str(self.root)
        self.folder = base / "home" / "dep-1"
        self.folder.mkdir(parents=True)
        for target in ("aws", "gcp", "local"):
            (self.folder / f"plan.{target}.json").write_text(json.dumps({"app": "demo-v2", "target": target}))
            (self.folder / f"build.{target}.json").write_text(json.dumps({"image": "app:" + SHA}))
        self.state = str(base / "home" / "state" / "demo-v2")

    def tearDown(self):
        if self.previous_root is None:
            os.environ.pop("INFRAMORPH_MODULES_ROOT", None)
        else:
            os.environ["INFRAMORPH_MODULES_ROOT"] = self.previous_root
        os.environ.pop("INFRAMORPH_LOCAL_PUBLISH", None)
        self.tmp.cleanup()

    def cmd(self, target, triggered_by="manual"):
        cwd, command = deployer_cmd({"id": "dep-1", "triggered_by": triggered_by}, target, self.folder)
        self.assertEqual(Path(cwd), self.root.resolve())
        self.assertEqual(command[:2], [sys.executable, "-m"])
        return command[2:]

    def test_aws_deploy_uses_aws_adapter_with_execute(self):
        os.environ["INFRAMORPH_LOCAL_PUBLISH"] = "1"
        self.assertEqual(self.cmd("aws"), [
            "adapters.aws", "deploy", "--plan", str(self.folder / "plan.aws.json"),
            "--artifact", str(self.folder / "build.aws.json"), "--state-dir", self.state,
            "--execute", "--deployment-id", "dep-1",
        ])

    def test_aws_rollback_needs_only_state_dir(self):
        self.assertEqual(self.cmd("aws", "rollback"), [
            "adapters.aws", "rollback", "--state-dir", self.state, "--execute", "--deployment-id", "dep-1",
        ])

    def test_gcp_deploy_and_rollback_require_execute(self):
        self.assertEqual(self.cmd("gcp"), [
            "adapters.gcp", "deploy", "--plan", str(self.folder / "plan.gcp.json"),
            "--artifact", str(self.folder / "build.gcp.json"), "--state-dir", self.state,
            "--execute", "--deployment-id", "dep-1",
        ])
        self.assertEqual(self.cmd("gcp", "rollback"), [
            "adapters.gcp", "rollback", "--state-dir", self.state,
            "--execute", "--deployment-id", "dep-1",
        ])

    def test_local_commands_are_unchanged(self):
        os.environ["INFRAMORPH_LOCAL_PUBLISH"] = "1"
        deploy = self.cmd("local")
        self.assertIn("--publish", deploy)
        self.assertNotIn("--execute", deploy)
        self.assertEqual(self.cmd("local", "rollback"),
                         ["adapters.local", "rollback", "--state-dir", self.state, "--deployment-id", "dep-1"])


if __name__ == "__main__":
    unittest.main()
