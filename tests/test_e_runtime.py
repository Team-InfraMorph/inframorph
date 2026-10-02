import copy
import difflib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adapters.local.runtime import (
    compose_document,
    deploy,
    env_file,
    lock_state,
    rollback,
    service_command,
)
from builder.runtime import build, check_build_profile
from policy_gate.gate import (
    PolicyError,
    digest,
    read_tree,
    sha,
    validate_intent,
    validate_patch,
)

ROOT = Path(__file__).resolve().parents[1]
REV = "a" * 40


def plan():
    p = json.loads((ROOT / "schemas/fixtures/v1/plan.local.json").read_text())
    p["source_revision"] = REV
    p["image_tag"] = "app:" + REV
    return p


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.original = self.root / "original"
        self.original.mkdir()
        (self.original / "src").mkdir()
        (self.original / "src/storage.js").write_text("module.exports = {};\n")
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        self.make_bundle({"src/storage.js": b"module.exports = { version: 2 };\n"})

    def make_bundle(self, after):
        import shutil

        if (self.bundle / "source").exists():
            shutil.rmtree(self.bundle / "source")
        (self.bundle / "source").mkdir()
        before, _ = read_tree(self.original, filter_source=True)
        chunks = []
        changes = []
        for name, data in after.items():
            p = self.bundle / "source" / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        for name in sorted(before.keys() | after.keys()):
            a, b = before.get(name), after.get(name)
            if a == b:
                continue
            chunks.extend(
                difflib.unified_diff(
                    (a or b"").decode().splitlines(True),
                    (b or b"").decode().splitlines(True),
                    fromfile="a/" + name if a is not None else "/dev/null",
                    tofile="b/" + name if b is not None else "/dev/null",
                )
            )
            changes.append(
                {
                    "path": name,
                    "action": "modify" if a is not None else "add",
                    "before_sha256": sha(a) if a else None,
                    "after_sha256": sha(b) if b else None,
                }
            )
        diff = "".join(chunks).encode()
        (self.bundle / "patch.diff").write_bytes(diff)
        report = {
            "schema_version": "1.0.0",
            "source_revision": REV,
            "target": "local",
            "requires_policy_gate": True,
            "original_digest": digest(before),
            "patched_digest": digest(after),
            "diff_sha256": sha(diff),
            "changes": changes,
            "status": "patched" if changes else "unchanged",
        }
        (self.bundle / "manifest.json").write_text(json.dumps(report))

    def intent(self):
        return {
            "source_revision": REV,
            "app": "demo",
            "runtime": "node22",
            "workloads": [
                {
                    "name": "web",
                    "kind": "http",
                    "public": True,
                    "port": 3000,
                    "health": "/health",
                    "evidence": ["src/storage.js:1"],
                }
            ],
            "state": [],
            "secrets": [],
            "config": {},
            "unknowns": [],
        }

    def test_manifest_version_rejected(self):
        report = json.loads((self.bundle / "manifest.json").read_text())
        report["schema_version"] = "2.0.0"
        (self.bundle / "manifest.json").write_text(json.dumps(report))
        with self.assertRaisesRegex(PolicyError, "manifest_version"):
            validate_patch(self.original, self.bundle, plan())

    def test_intent_valid(self):
        self.assertEqual(
            validate_intent(self.intent(), self.original, REV).runtime, "node22"
        )

    def test_missing_evidence(self):
        for evidence in ["src/missing.js:1", "src/storage.js:999", "../secret:1"]:
            x = self.intent()
            x["workloads"][0]["evidence"] = [evidence]
            with self.subTest(evidence=evidence), self.assertRaises(PolicyError):
                validate_intent(x, self.original, REV)

    def test_unknown_or_revision(self):
        x = self.intent()
        x["unknowns"] = ["unknown"]
        with self.assertRaises(PolicyError):
            validate_intent(x, self.original, REV)
        with self.assertRaises(PolicyError):
            validate_intent(self.intent(), self.original, "b" * 40)

    def test_excess_public(self):
        x = self.intent()
        second = copy.deepcopy(x["workloads"][0])
        second["name"] = "admin"
        x["workloads"].append(second)
        with self.assertRaises(PolicyError):
            validate_intent(x, self.original, REV)

    def test_patch_valid_and_immutable(self):
        result = validate_patch(self.original, self.bundle, plan())
        (self.bundle / "source/src/storage.js").write_text("eval('1')\n")
        self.assertIn(b"version", result.files["src/storage.js"])
        with self.assertRaises(TypeError):
            result.files["foo"] = b"bad"

    def test_tampered_content(self):
        (self.bundle / "source/src/storage.js").write_text("unexpected\n")
        with self.assertRaisesRegex(PolicyError, "digest"):
            validate_patch(self.original, self.bundle, plan())

    def test_manifest_and_diff_must_agree_with_source(self):
        report = json.loads((self.bundle / "manifest.json").read_text())
        report["diff_sha256"] = sha(b"")
        (self.bundle / "patch.diff").write_bytes(b"")
        (self.bundle / "manifest.json").write_text(json.dumps(report))
        with self.assertRaisesRegex(PolicyError, "diff_source"):
            validate_patch(self.original, self.bundle, plan())

    def test_added_deleted_and_renamed_paths(self):
        for files in [
            {"src/storage.js": b"module.exports={}\n", "evil.js": b"bad\n"},
            {"src/storage.js": b"module.exports={}\n", "src/renamed.js": b"bad\n"},
        ]:
            self.make_bundle(files)
            with self.assertRaisesRegex(PolicyError, "allowlist"):
                validate_patch(self.original, self.bundle, plan())
        self.make_bundle({})
        with self.assertRaises(PolicyError):
            validate_patch(self.original, self.bundle, plan())

    def test_forbidden_and_syntax(self):
        for source, code in [
            (b"eval('1')\n", "forbidden"),
            (b"require('child_process')\n", "forbidden"),
            (b"function bad( {\n", "syntax"),
        ]:
            self.make_bundle({"src/storage.js": source})
            with self.subTest(source=source), self.assertRaisesRegex(PolicyError, code):
                validate_patch(self.original, self.bundle, plan())

    def test_symlink_and_hardlink(self):
        p = self.bundle / "source/src/storage.js"
        p.unlink()
        p.symlink_to("/etc/hosts")
        with self.assertRaises(PolicyError):
            validate_patch(self.original, self.bundle, plan())
        p.unlink()
        os.link(self.original / "src/storage.js", p)
        with self.assertRaises(PolicyError):
            validate_patch(self.original, self.bundle, plan())

    def test_sensitive_file_not_built(self):
        self.make_bundle(
            {"src/storage.js": b"module.exports={}\n", ".env": b"TOKEN=notreal\n"}
        )
        with self.assertRaises(PolicyError):
            validate_patch(self.original, self.bundle, plan())

    def test_rejected_gate_never_invokes_docker(self):
        calls = []
        (self.bundle / "patch.diff").write_text("corrupt")
        with self.assertRaises(PolicyError):
            build(
                self.original,
                self.bundle,
                plan(),
                execute=lambda *a, **k: calls.append(a),
            )
        self.assertEqual(calls, [])

    def test_build_profile_requires_reviewed_lock(self):
        with self.assertRaises(PolicyError):
            check_build_profile({"package.json": b"{}"})

    def test_tag_collision_rejected(self):
        info = {"Os": "linux", "Architecture": "amd64", "Config": {"Labels": {}}}
        responses = iter(["linux", "existing", json.dumps([info])])
        calls = []

        def execute(args, **kw):
            calls.append(args)
            return next(responses)

        with (
            patch("builder.runtime.check_build_profile"),
            self.assertRaisesRegex(PolicyError, "collision"),
        ):
            build(self.original, self.bundle, plan(), execute=execute)
        self.assertFalse(any("buildx" in c for c in calls))


class LocalTests(unittest.TestCase):
    def test_private_ports_and_named_volumes(self):
        document = compose_document(plan(), "sha256:" + "b" * 64, "inframorph-demo-app")
        self.assertNotIn("ports", document["services"]["db"])
        self.assertNotIn("privileged", document["services"]["web"])
        self.assertEqual(document["services"]["web"]["ports"], ["127.0.0.1::3000"])
        self.assertNotIn(
            "--accept-data-loss", document["services"]["schema"]["command"]
        )
        self.assertEqual(
            document["volumes"]["uploads"]["name"], "inframorph-demo-app-uploads"
        )

    def test_worker_no_shell_or_port(self):
        p = plan()
        p["services"].append(
            {
                "name": "worker",
                "kind": "worker",
                "cpu": 256,
                "mem": 512,
                "public": False,
                "command": "node src/worker.js",
            }
        )
        worker = compose_document(p, "sha256:" + "b" * 64, "inframorph-demo-app")[
            "services"
        ]["worker"]
        self.assertEqual(worker["command"], ["node", "src/worker.js"])
        self.assertNotIn("ports", worker)
        for command in [
            "sh -c whoami",
            "node src/worker.js; echo bad",
            "node ../../secret.js",
        ]:
            with self.assertRaises(PolicyError):
                service_command(command)

    def test_secret_file_permissions_and_injection(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "app.env"
            env_file(p, {"TOKEN": "dummy"})
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(PolicyError):
                env_file(p, {"TOKEN": "bad\nINJECT=1"})

    def test_state_lock(self):
        with tempfile.TemporaryDirectory() as t:
            with lock_state(Path(t).resolve() / "demo"):
                with self.assertRaises(RuntimeError):
                    with lock_state(Path(t).resolve() / "demo"):
                        pass

    def test_invalid_artifact_never_executes(self):
        p = plan()
        a = {
            "source_revision": "b" * 40,
            "target": "local",
            "image": "app:" + "b" * 40,
            "platform": "linux/amd64",
        }
        with self.assertRaises(PolicyError):
            deploy(
                p, a, "unused", execute=lambda *a, **k: self.fail("Docker must not run")
            )

    def test_no_rollback_point(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(PolicyError):
                rollback(
                    Path(t).resolve() / "demo",
                    execute=lambda *a, **k: self.fail("Docker must not run"),
                )


if __name__ == "__main__":
    unittest.main()
