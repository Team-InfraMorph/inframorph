import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from repo_mapper import MapperError, map_repository
from repo_mapper.rules import build_repo_map


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/analyzer"
URL = "https://github.com/Team-InfraMorph/demo-app"


def git(*args, cwd):
    env = {"PATH": os.environ["PATH"], "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True).stdout.decode().strip()


class RepoMapperTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        self.count = 0

    def repo(self, case="v2", extra=None, symlink=None):
        work = self.temp / "work"
        shutil.copytree(FIXTURES / case / "snapshot", work)
        for path, data in (extra or {}).items():
            (work / path).parent.mkdir(parents=True, exist_ok=True)
            (work / path).write_bytes(data)
        if symlink:
            os.symlink(symlink[1], work / symlink[0])
        git("init", "-q", "-b", "main", cwd=work)
        git("add", "-A", cwd=work)
        git("commit", "-q", "-m", "fixture", cwd=work)
        return work, git("rev-parse", "HEAD", cwd=work)

    def output(self):
        self.count += 1
        folder = self.temp / f"out{self.count}"
        folder.mkdir()
        return folder

    def request(self, output, revision=None, branch="main"):
        return {"repo_url": URL, "branch": branch, "source_revision": revision, "output_dir": str(output)}

    def test_fixture_sources_map_to_contract_repo_map(self):
        for case in ("v1", "v2"):
            with self.subTest(case=case):
                expected = json.loads((FIXTURES / case / "repo_map.json").read_text())
                files = {p.relative_to(FIXTURES / case / "snapshot").as_posix(): p.read_bytes()
                         for p in (FIXTURES / case / "snapshot").rglob("*") if p.is_file()}
                first = build_repo_map(expected["commit"], files).model_dump(mode="json")
                again = build_repo_map(expected["commit"], dict(reversed(files.items()))).model_dump(mode="json")
                self.assertEqual(first, expected)
                self.assertEqual(json.dumps(first), json.dumps(again))

    def test_snapshot_is_exact_selected_and_read_only(self):
        extra = {".github/workflows/ci.yml": b"on: push\n", "scripts/tool.py": b"print(1)\n",
                 "tests/test_x.py": b"pass\n", ".env": b"X=1\n", "README.md": b"# demo\n"}
        work, sha = self.repo(extra=extra)
        mapped = map_repository(self.request(self.output(), sha), remote=str(work))
        self.assertEqual(mapped.repo_map.commit, sha)
        self.assertEqual(mapped.snapshot.name, "snapshot")
        files = sorted(p.relative_to(mapped.snapshot).as_posix() for p in mapped.snapshot.rglob("*") if p.is_file())
        self.assertEqual(files, mapped.repo_map.tree)
        self.assertEqual(files, ["package.json", "prisma/schema.prisma", "src/images.js", "src/server.js", "src/worker.js"])
        for name in files:
            self.assertEqual((mapped.snapshot / name).read_bytes(), (work / name).read_bytes())
            self.assertFalse(os.stat(mapped.snapshot / name).st_mode & 0o222)

    def test_null_revision_resolves_branch_head(self):
        work, sha = self.repo()
        mapped = map_repository(self.request(self.output()), remote=str(work))
        self.assertEqual(mapped.repo_map.commit, sha)

    def test_failures_use_fixed_codes_and_leave_no_snapshot(self):
        work, sha = self.repo()
        cases = [
            ("revision_not_found", self.request(self.output(), "0" * 40), str(work)),
            ("branch_not_found", self.request(self.output(), branch="missing"), str(work)),
            ("clone_failed", self.request(self.output()), str(self.temp / "nope")),
        ]
        for code, request, remote in cases:
            with self.subTest(code=code), self.assertRaisesRegex(MapperError, f"^{code}$"):
                map_repository(request, remote=remote)
            self.assertFalse((Path(request["output_dir"]) / "snapshot").exists())

    def test_invalid_requests_are_rejected_before_fetch(self):
        output = self.output()
        base = self.request(output, "a" * 40)
        invalid = [base | {"repo_url": "https://gitlab.com/a/b"}, base | {"repo_url": "file:///tmp/x"},
                   base | {"branch": "../x"}, base | {"branch": "-x"}, base | {"source_revision": "abc"},
                   base | {"output_dir": "relative"}, base | {"extra": 1}, {"repo_url": URL}]
        for request in invalid:
            with self.subTest(request=request), self.assertRaisesRegex(MapperError, "^invalid_(request|output_dir)$"):
                map_repository(request, remote=str(self.temp / "never-used"))

    def test_symlinked_output_directory_is_rejected(self):
        work, sha = self.repo()
        link = self.temp / "link"
        os.symlink(self.output(), link)
        with self.assertRaisesRegex(MapperError, "^invalid_output_dir$"):
            map_repository(self.request(link, sha), remote=str(work))

    def test_unsafe_selected_files_are_rejected(self):
        cases = {
            "symlink_rejected": {"symlink": ("src/link.js", "/etc/passwd")},
            "unsupported_file": {"extra": {"src/blob.js": b"\x00\x01"}},
            "secret_detected": {"extra": {"src/key.js": b"const API_KEY = 'sk-abcdefghijklmnopqrstuvwx';\n"}},
            "snapshot_limit": {"extra": {"src/big.js": b"x" * 300_000}},
        }
        for code, options in cases.items():
            with self.subTest(code=code):
                shutil.rmtree(self.temp / "work", ignore_errors=True)
                work, sha = self.repo(**options)
                output = self.output()
                with self.assertRaisesRegex(MapperError, f"^{code}$"):
                    map_repository(self.request(output, sha), remote=str(work))
                self.assertFalse((output / "snapshot").exists())

    def test_symlink_outside_selected_paths_is_not_copied(self):
        work, sha = self.repo(symlink=("docs-link", "/etc/passwd"))
        mapped = map_repository(self.request(self.output(), sha), remote=str(work))
        self.assertNotIn("docs-link", mapped.repo_map.tree)

    def test_cli_reports_only_fixed_error_code(self):
        result = subprocess.run([sys.executable, "-m", "repo_mapper"], cwd=ROOT, input=b'{"repo_url": 1}',
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr.strip(), b"invalid_request")


if __name__ == "__main__":
    unittest.main()
