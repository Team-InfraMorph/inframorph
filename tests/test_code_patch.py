from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from code_patch.runner import ALLOWED_PATHS, PatchError, TEMPLATES, make_diff, patch_snapshot
from code_patch.local_smoke import SmokeError, preflight

ROOT = Path(__file__).resolve().parents[1]


class CodePatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name).resolve()
        self.source = self.work / "original"
        shutil.copytree(ROOT / "tests/fixtures/analyzer/v1/snapshot", self.source)
        self.mapping = json.loads((ROOT / "tests/fixtures/analyzer/v1/repo_map.json").read_text())
        self.plan = json.loads((ROOT / "schemas/fixtures/v1/plan.local.json").read_text())

    def patch(self, name="patched", plan=None):
        return patch_snapshot(self.source, self.mapping, plan or self.plan, self.work / name)

    def test_copy_diff_and_original_immutability(self):
        before = {p.relative_to(self.source): p.read_bytes() for p in self.source.rglob("*") if p.is_file()}
        report = self.patch()
        self.assertEqual(report["status"], "patched")
        self.assertEqual({x["path"] for x in report["changes"]}, ALLOWED_PATHS)
        for name, contents in before.items():
            self.assertEqual((self.source / name).read_bytes(), contents)
        patched = self.work / "patched/source"
        self.assertIn('provider = "postgresql"', (patched / "prisma/schema.prisma").read_text())
        self.assertEqual((patched / "src/server.js").read_bytes(), before[Path("src/server.js")])
        lock = json.loads((patched / "package-lock.json").read_text())
        package = json.loads((patched / "package.json").read_text())
        self.assertEqual(lock["packages"][""]["dependencies"], package["dependencies"])
        applied = self.work / "applied"
        shutil.copytree(self.source, applied)
        subprocess.run(
            [
                "git",
                "-c",
                "core.autocrlf=false",
                "apply",
                str(self.work / "patched/patch.diff"),
            ],
            cwd=applied,
            capture_output=True,
            check=True,
        )
        self.assertEqual({p.relative_to(applied): p.read_bytes() for p in applied.rglob("*") if p.is_file()},
                         {p.relative_to(patched): p.read_bytes() for p in patched.rglob("*") if p.is_file()})

    def test_local_and_aws_emit_identical_code_and_lockfile(self):
        local = self.patch()
        aws = json.loads((ROOT / "schemas/fixtures/v1/plan.aws.json").read_text())
        self.assertEqual(local["patched_digest"], self.patch("aws", aws)["patched_digest"])

    def test_repeat_patch_is_unchanged(self):
        self.patch()
        self.source = self.work / "patched/source"
        self.mapping["tree"] = sorted(p.relative_to(self.source).as_posix()
                                      for p in self.source.rglob("*") if p.is_file())
        report = self.patch("again")
        self.assertEqual(report["status"], "unchanged")
        self.assertEqual(report["changes"], [])
        self.assertEqual((self.work / "again/patch.diff").read_text(), "")

    def test_no_patch_plan_copies_without_changes(self):
        plan = deepcopy(self.plan)
        plan.update(db=None, storage=None, config={})
        self.assertEqual(self.patch(plan=plan)["status"], "unchanged")

    def test_only_requested_patch_targets_change(self):
        plan = deepcopy(self.plan)
        plan.update(storage=None, config={})
        self.assertEqual([c["path"] for c in self.patch("db-only", plan)["changes"]], ["prisma/schema.prisma"])
        plan = deepcopy(self.plan)
        plan["db"] = None
        self.assertNotIn("prisma/schema.prisma", [c["path"] for c in self.patch("storage-only", plan)["changes"]])

    def test_preflight_rejects_source_or_diff_tampering_before_build(self):
        report = self.patch()
        source = self.work / "patched/source"
        path = source / "src/server.js"
        before = path.read_bytes()
        path.write_bytes(before + b"\n// unauthorized change\n")
        with self.assertRaisesRegex(SmokeError, "preflight_digest_or_allowlist_failure"):
            preflight(source, self.source, report)
        path.write_bytes(before)
        (self.work / "patched/patch.diff").write_text("tampered")
        with self.assertRaisesRegex(SmokeError, "preflight_diff_digest_failure"):
            preflight(source, self.source, report)

    def test_existing_demo_lockfile_is_updated(self):
        shutil.copyfile(TEMPLATES / "base-package-lock.json", self.source / "package-lock.json")
        self.mapping["tree"].append("package-lock.json")
        report = self.patch()
        self.assertEqual(next(x["action"] for x in report["changes"] if x["path"] == "package-lock.json"), "modify")

    def test_revision_mismatch_never_creates_output(self):
        self.mapping["commit"] = "a" * 40
        with self.assertRaisesRegex(PatchError, "revision_mismatch"):
            self.patch()
        self.assertFalse((self.work / "patched").exists())

    def test_unsupported_source_never_leaves_partial_output(self):
        path = self.source / "src/images.js"
        path.write_text(path.read_text() + "\n// changed behavior\n")
        with self.assertRaisesRegex(PatchError, "unsupported_image_callsites"):
            self.patch()
        self.assertFalse((self.work / "patched").exists())

    def test_unsupported_schema_storage_dependency_and_lock(self):
        for name, contents in (("prisma/schema.prisma", "model Changed {}"),
                               ("src/storage.js", "unrelated module"),
                               ("package.json", '{}'), ("package.json", '[]'),
                               ("package.json", 'null'), ("package-lock.json", '{}')):
            with self.subTest(name=name):
                path = self.source / name
                previous = path.read_bytes() if path.exists() else None
                path.write_text(contents)
                if name not in self.mapping["tree"]:
                    self.mapping["tree"].append(name)
                with self.assertRaises(PatchError):
                    self.patch()
                if previous is None:
                    path.unlink()
                    self.mapping["tree"].remove(name)
                else:
                    path.write_bytes(previous)

    def test_unmapped_file_is_not_silently_dropped(self):
        (self.source / "required.js").write_text("module.exports = 1;")
        with self.assertRaisesRegex(PatchError, "unmapped_source_file"):
            self.patch()

    def test_secrets_excluded_or_blocked_without_echoing(self):
        secret = "sk-test-not-a-real-secret-value-abcdefghijklmnop"
        (self.source / ".env").write_text("OPENAI_API_KEY=" + secret)
        self.mapping["tree"].append(".env")
        report = self.patch()
        self.assertEqual(report["excluded_paths"], [".env"])
        self.assertFalse((self.work / "patched/source/.env").exists())
        self.assertNotIn(secret, (self.work / "patched/patch.diff").read_text())
        (self.source / "leak.js").write_text('const API_KEY = "' + secret + '";')
        self.mapping["tree"].append("leak.js")
        with self.assertRaisesRegex(PatchError, "secret_in_source"):
            self.patch("blocked")

    def test_symlink_hardlink_traversal_and_existing_destination(self):
        outside = self.work / "outside.js"
        outside.write_text("private file")
        original = self.source / "src/images.js"
        saved = original.read_bytes()
        original.unlink()
        for kind in ("symlink", "hardlink"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    original.symlink_to(outside)
                else:
                    original.hardlink_to(outside)
                with self.assertRaises(PatchError):
                    self.patch()
                original.unlink()
        original.write_bytes(saved)
        self.mapping["tree"].append("../outside.js")
        with self.assertRaises(PatchError):
            self.patch()
        self.mapping["tree"].pop()
        self.patch()
        with self.assertRaisesRegex(PatchError, "output_must_be_new"):
            self.patch()

    def test_allowlist_rejects_unrelated_edits_and_deletes(self):
        for before, after in (({}, {"src/server.js": b"changed"}),
                              ({"src/images.js": b"old"}, {})):
            with self.assertRaisesRegex(PatchError, "patch_allowlist_violation"):
                make_diff(before, after)

    @unittest.skipUnless(shutil.which("node"), "Node is required for storage template tests")
    def test_storage_runtime_without_network(self):
        result = subprocess.run(["node", "--test", str(ROOT / "tests/test_storage.cjs")],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
