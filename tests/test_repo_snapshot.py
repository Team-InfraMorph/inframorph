from pathlib import Path
import subprocess
import tempfile
import unittest

from repo_mapper.snapshot import _safe_name, snapshot_from_checkout, snapshot_from_remote


def fixture_repo(root):
    repo = root / "repo"
    repo.mkdir()
    files = {
        "package.json": '{"name":"demo-app"}\n',
        "package-lock.json": "{}\n",
        "prisma/schema.prisma": (
            'provider = "sqlite"\nurl = env("DATABASE_URL")\n'
        ),
        "src/images.js": "fs.writeFile()\n",
        "src/server.js": "app.get('/health', f)\n",
        "README.md": "Included in the commit\n",
    }
    for name, data in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
    git(repo, "init", "--quiet")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "core.autocrlf", "false")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "fixture")
    return repo, git(repo, "rev-parse", "HEAD"), files


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True,
        capture_output=True, text=True,
    ).stdout.strip()


class SnapshotTests(unittest.TestCase):
    def test_traversal_paths_are_rejected(self):
        for name in (b"../outside", b"src/../../outside", b".git/config", b"src\\outside"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "unsafe_source_path"):
                _safe_name(name)
        self.assertEqual(_safe_name(b".github/workflows/ci.yml"), ".github/workflows/ci.yml")

    def test_exact_commit_and_full_read_only_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, revision, files = fixture_repo(root)
            output = root / "output"
            output.mkdir()
            # The working tree can differ; the snapshot must read Git blobs.
            (repo / "README.md").write_text("uncommitted change\n")
            result = snapshot_from_checkout(repo, revision, output)
            self.assertEqual(result.commit, revision)
            self.assertEqual(set(result.files), set(files))
            self.assertEqual(
                (result.path / "README.md").read_text(), files["README.md"]
            )
            self.assertEqual(
                (result.path / "README.md").stat().st_mode & 0o222, 0
            )

    def test_wrong_revision_and_existing_output_leave_no_partial_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, _, _ = fixture_repo(root)
            output = root / "output"
            output.mkdir()
            with self.assertRaises(subprocess.CalledProcessError):
                snapshot_from_checkout(repo, "0" * 40, output)
            self.assertFalse((output / "snapshot").exists())
            snapshot_from_checkout(repo, None, output)
            with self.assertRaisesRegex(ValueError, "output_exists"):
                snapshot_from_checkout(repo, None, output)

    def test_symlink_in_commit_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, _, _ = fixture_repo(root)
            (repo / "link").symlink_to("README.md")
            git(repo, "add", "link")
            git(repo, "commit", "--quiet", "-m", "symlink")
            output = root / "output"
            output.mkdir()
            with self.assertRaisesRegex(ValueError, "unsupported_file_type"):
                snapshot_from_checkout(repo, None, output)
            self.assertFalse((output / "snapshot").exists())

    def test_remote_clone_and_branch_revision_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, revision, _ = fixture_repo(root)
            branch = git(repo, "branch", "--show-current")
            output = root / "output"
            output.mkdir()
            snapshot = snapshot_from_remote(str(repo), branch, revision, output)
            self.assertEqual(snapshot.commit, revision)

            other = root / "other"
            other.mkdir()
            with self.assertRaises(subprocess.CalledProcessError):
                snapshot_from_remote(str(repo), "missing-branch", revision, other)
            self.assertFalse((other / "snapshot").exists())


if __name__ == "__main__":
    unittest.main()
