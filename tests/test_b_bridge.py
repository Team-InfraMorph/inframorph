import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from control_plane.b_bridge import BCommandError, BCommands, LIMIT, call_json


def command(source):
    return [sys.executable, "-c", source]


class BBridgeTests(unittest.TestCase):
    def test_json_data_is_not_interpreted_and_api_credentials_are_not_inherited(self):
        value = {"repo_url": "$(echo private); `echo private`", "branch": "feature with spaces"}
        source = "import json,os,sys; value=json.load(sys.stdin); value['key_present']='OPENAI_API_KEY' in os.environ; print(json.dumps(value))"
        with patch.dict(os.environ, {"OPENAI_API_KEY": "private-test-canary", "OPENAI_BASE_URL": "https://invalid.example"}):
            result = call_json(command(source), value)
        self.assertEqual(result, value | {"key_present": False})

    def test_invalid_command_and_unavailable_program_have_fixed_errors(self):
        for invalid in ("python something", [], [sys.executable, "\x00"], [False]):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(BCommandError, "^invalid_b_command$"):
                call_json(invalid, {})
        with self.assertRaisesRegex(BCommandError, "^b_command_unavailable$"):
            call_json(["/inframorph-nonexistent-b-program"], {})
        with self.assertRaisesRegex(BCommandError, "^invalid_b_command$"):
            BCommands(mapper_command="python module", planner_command=command("pass"))

    def test_input_limit_prevents_starting_command(self):
        with patch("control_plane.b_bridge.subprocess.Popen") as start:
            with self.assertRaisesRegex(BCommandError, "^b_request_limit$"):
                call_json(command("pass"), {"data": "x" * LIMIT})
        start.assert_not_called()

    def test_output_limit_stops_live_producer_without_waiting_for_exit(self):
        source = "import os,time; os.write(1,b'x'*120001); time.sleep(30)"
        started = time.monotonic()
        with self.assertRaisesRegex(BCommandError, "^b_reply_limit$"):
            call_json(command(source), {}, timeout=2)
        self.assertLess(time.monotonic() - started, 2)

    def test_simultaneous_large_input_and_output_do_not_deadlock(self):
        source = "import os,time; os.write(1,b'x'*120001); time.sleep(30)"
        with self.assertRaisesRegex(BCommandError, "^b_reply_limit$"):
            call_json(command(source), {"data": "x" * 100_000}, timeout=2)

    def test_invalid_duplicate_non_finite_and_private_output_is_not_exposed(self):
        for raw in (b"", b"private-test-canary", b'{"port":1,"port":2}', b'{"value":NaN}', b'\xff'):
            with self.subTest(raw=raw), self.assertRaisesRegex(BCommandError, "^invalid_b_json$"):
                call_json(command(f"import os; os.write(1,{raw!r})"), {})
        with self.assertRaisesRegex(BCommandError, "^b_command_failed$"):
            call_json(command("import sys; print('private-test-canary'); sys.exit(1)"), {})

    def test_silent_and_closed_stdout_processes_obey_deadline(self):
        for source in ("import time; time.sleep(30)", "import os,time; os.close(1); time.sleep(30)"):
            started = time.monotonic()
            with self.subTest(source=source), self.assertRaisesRegex(BCommandError, "^b_command_timeout$"):
                call_json(command(source), {}, timeout=0.15)
            self.assertLess(time.monotonic() - started, 2)

    def test_timeout_kills_descendants_even_when_their_stdout_is_detached(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "descendant-ran"
            ready = Path(temp) / "ready"
            child = f"import time; from pathlib import Path; time.sleep(1); Path({str(marker)!r}).touch()"
            source = (f"import subprocess,sys,time; from pathlib import Path; subprocess.Popen([sys.executable,'-c',{child!r}],"
                f"stdout=subprocess.DEVNULL); Path({str(ready)!r}).touch(); time.sleep(30)")
            with self.assertRaisesRegex(BCommandError, "^b_command_timeout$"):
                call_json(command(source), {}, timeout=0.3)
            self.assertTrue(ready.exists())
            time.sleep(1)
            self.assertFalse(marker.exists())

    def test_success_also_stops_detached_background_descendants(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "descendant-ran"
            child = f"import time; from pathlib import Path; time.sleep(1); Path({str(marker)!r}).touch()"
            source = (f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{child!r}],stdout=subprocess.DEVNULL); print('{{}}')")
            self.assertEqual(call_json(command(source), {}), {})
            time.sleep(1)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
