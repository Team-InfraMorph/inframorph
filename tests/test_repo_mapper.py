import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from repo_mapper.__main__ import main
from repo_mapper.mapper import map_snapshot
from repo_mapper.snapshot import snapshot_from_checkout
from test_repo_snapshot import fixture_repo, git

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT.parent / "demo-app"


class RepoMapperTests(unittest.TestCase):
    def test_cli_clones_and_returns_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, revision, _ = fixture_repo(root)
            branch = git(repo, "branch", "--show-current")
            (repo / "package.json").write_text(json.dumps({
                "name": "demo-app",
                "scripts": {"start": "node src/server.js"},
            }))
            git(repo, "add", "package.json")
            git(repo, "commit", "--quiet", "-m", "package")
            revision = git(repo, "rev-parse", "HEAD")
            output = root / "output"
            output.mkdir()
            request = json.dumps({
                "repo_url": str(repo), "branch": branch,
                "source_revision": revision, "output_dir": str(output),
            })
            stdout = io.StringIO()
            with patch("repo_mapper.__main__.URLS", {str(repo)}), \
                 patch("sys.stdin", io.StringIO(request)), \
                 patch("sys.stdout", stdout):
                self.assertEqual(main(), 0)
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["repo_map"]["commit"], revision)
            self.assertEqual(result["snapshot"], "snapshot")

    def test_full_tree_and_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, revision, files = fixture_repo(root)
            (repo / "package.json").write_text(json.dumps({
                "name": "demo-app",
                "scripts": {"start": "node src/server.js"},
                "dependencies": {"express": "1"},
            }))
            git(repo, "add", "package.json")
            git(repo, "commit", "--quiet", "-m", "package")
            revision = git(repo, "rev-parse", "HEAD")
            output = root / "output"
            output.mkdir()
            snapshot = snapshot_from_checkout(repo, revision, output)
            mapping = map_snapshot(snapshot, output)
            self.assertEqual(mapping.tree, sorted(files))
            self.assertIn("README.md", mapping.tree)
            self.assertEqual(mapping.routes, ["GET /health"])
            self.assertEqual(mapping.hints[0].at, "src/images.js:1")
            self.assertEqual(
                json.loads((output / "repo_map.json").read_text())["commit"],
                revision,
            )

    @unittest.skipUnless(DEMO.is_dir(), "sibling demo-app unavailable")
    def test_real_v1_v2_match_reviewed_fixtures(self):
        for version in ("v1", "v2"):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary)
                revision = git(DEMO, "rev-parse", f"demo-{version}^{{commit}}")
                snapshot = snapshot_from_checkout(DEMO, revision, output)
                actual = map_snapshot(snapshot, output).model_dump(mode="json")
                expected = json.loads((
                    ROOT / "schemas" / "fixtures" / version / "repo_map.json"
                ).read_text())
                self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
