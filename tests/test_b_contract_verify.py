import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

from control_plane.b_bridge import BCommands
from control_plane.b_contract_verify import fixture_commands, main, verify


class BContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.commands = fixture_commands()

    def modules(self, **overrides):
        commands = self.commands | overrides
        return BCommands(mapper_command=commands["mapper"], planner_command=commands["planner"])

    def test_real_command_bridge_c_tool_loop_and_e_gates_pass_both_cases(self):
        output = self.root / "passed"
        summary = verify(self.modules(), output, fixture_mode=True)
        self.assertEqual(summary["status"], "passed")
        self.assertEqual(summary["b_commands"], "fixture command stand-ins")
        self.assertFalse(summary["team_api_called"] or summary["docker_called"] or summary["aws_called"])
        for report in summary["results"]:
            self.assertEqual(len(report["checks"]), 6)
            self.assertGreater(report["metrics"]["tool_calls"], 0)
            self.assertEqual(report["metrics"]["api_calls"], 0)
            self.assertTrue((output / report["case"] / "patch/manifest.json").is_file())
            self.assertEqual((output / report["case"] / "report.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads((output / "summary.json").read_text()), summary)

    def test_wrong_mapper_revision_stops_before_c_analysis(self):
        source = ("import json,sys; from pathlib import Path; from control_plane.b_bridge import DemoModules; "
            "r=json.load(sys.stdin); m=DemoModules().map(r,{'commit_sha':r['source_revision']},Path(r['output_dir'])); "
            "value=m.repo_map.model_dump(mode='json'); value['commit']='f'*40; print(json.dumps({'snapshot':'snapshot','repo_map':value}))")
        result = verify(self.modules(mapper=[sys.executable, "-c", source]), self.root / "wrong-sha", cases=("v1",))
        report = result["results"][0]
        self.assertEqual(report["stage"], "mapper")
        self.assertEqual(report["checks"], [])
        self.assertFalse((self.root / "wrong-sha/v1/patch").exists())

    def test_unsafe_planner_config_stops_before_patching(self):
        source = ("import json,sys; from schemas import Intent; from control_plane.b_bridge import DemoModules; "
            "r=json.load(sys.stdin); p=DemoModules().plan(Intent.model_validate(r['intent'])).model_dump(mode='json'); "
            "p['config']['NODE_OPTIONS']='--require ./src/server.js'; print(json.dumps(p))")
        output = self.root / "unsafe-plan"
        result = verify(self.modules(planner=[sys.executable, "-c", source]), output, cases=("v1",))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["results"][0]["stage"], "planner")
        self.assertFalse((output / "v1/patch").exists())
        self.assertFalse((output / "v1/plan.json").exists())

    def test_raw_command_output_and_stderr_are_not_saved_in_failure_report(self):
        source = "import sys; print('private-output-canary'); print('private-stderr-canary',file=sys.stderr)"
        output = self.root / "invalid"
        result = verify(self.modules(mapper=[sys.executable, "-c", source]), output, cases=("v1",))
        self.assertEqual(result["results"][0]["error"], "invalid_b_json")
        stored = (output / "summary.json").read_text() + (output / "v1/report.json").read_text()
        self.assertNotIn("private-output-canary", stored)
        self.assertNotIn("private-stderr-canary", stored)

    def test_cli_never_implicitly_falls_back_to_fixture_commands(self):
        for args in ([], ["--mapper-command", '["python"]'],
                     ["--fixture-commands", "--mapper-command", '["python"]']):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                main(args)
            self.assertEqual(raised.exception.code, 2)

    def test_output_cannot_overwrite_a_previous_run_or_follow_symlink(self):
        output = self.root / "existing"
        output.mkdir()
        with self.assertRaises(FileExistsError):
            verify(self.modules(), output)
        linked = self.root / "linked"
        linked.symlink_to(output, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "contract_output_symlink"):
            verify(self.modules(), linked / "new")


if __name__ == "__main__":
    unittest.main()
