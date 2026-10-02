import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest

from fastapi.testclient import TestClient
from analyzer.recovery import Approval, PatchedCandidate
from code_patch import patch_snapshot
from control_plane.app import create_app
from control_plane.db import Store
from policy_gate.gate import validate_patch
from schemas import Plan, RepoMap


ROOT = Path(__file__).resolve().parents[1]


class PatchReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.mapping = RepoMap.model_validate_json((ROOT / "tests/fixtures/analyzer/v1/repo_map.json").read_text())
        self.plan = Plan.model_validate_json((ROOT / "schemas/fixtures/v1/plan.local.json").read_text())
        self.source = self.root / "snapshot"
        shutil.copytree(ROOT / "tests/fixtures/analyzer/v1/snapshot", self.source)
        self.bundle = self.root / "patch"
        manifest = patch_snapshot(self.source, self.mapping, self.plan, self.bundle)
        validate_patch(self.source, self.bundle, self.plan)
        files = tuple(sorted(set(self.mapping.tree) | {c["path"] for c in manifest["changes"]}))
        self.candidate = PatchedCandidate(self.bundle, manifest, self.plan, files)
        self.receipt = Approval(approved=True, fingerprint=self.candidate.fingerprint)
        self.store = Store(self.root / "cp.db")
        self.project = self.store.create_project("https://github.com/Team-InfraMorph/demo-app", "main", ["local"])
        self.did = self.store.begin_deploy(self.project["project_id"])
        self.store.set_commit(self.did, self.mapping.commit)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def save(self, phase="initial", receipt=None):
        return self.store.save_validated_patch(self.did, self.candidate, self.mapping,
            receipt or self.receipt, phase)

    def test_projection_matches_verified_manifest_and_omits_lock_diff(self):
        self.save()
        value = self.store.get_runtime_patches(self.did)["local"]
        self.assertEqual(value["diff_sha256"], self.candidate.manifest["diff_sha256"])
        self.assertEqual(value["fingerprint"], self.candidate.fingerprint)
        self.assertTrue(value["verified"])
        self.assertFalse(value["applied"])
        lock = next(f for f in value["files"] if f["path"] == "package-lock.json")
        self.assertIsNone(lock["diff"])
        prisma = next(f for f in value["files"] if f["path"] == "prisma/schema.prisma")
        self.assertIn('+    provider = "postgresql"', prisma["diff"])
        self.assertNotIn(str(self.root), json.dumps(value))

    def test_denied_or_wrong_receipt_cannot_publish(self):
        for receipt in (Approval(approved=False, fingerprint=self.candidate.fingerprint),
                        Approval(approved=True, fingerprint="0" * 64)):
            with self.assertRaises(ValueError):
                self.save(receipt=receipt)
        self.assertEqual(self.store.get_runtime_patches(self.did), {})

    def test_changed_bundle_before_publish_is_rejected(self):
        (self.bundle / "patch.diff").write_text("+++ b/private.txt\n+not approved\n")
        with self.assertRaises(ValueError):
            self.save()
        self.assertEqual(self.store.get_runtime_patches(self.did), {})

    def test_api_reads_immutable_record_after_bundle_deleted(self):
        self.save()
        before = self.store.get_runtime_patches(self.did)
        shutil.rmtree(self.bundle)
        app = create_app(db_path=self.store.path, runtime=object())
        with TestClient(app) as client:
            response = client.get(f"/api/deployments/{self.did}/patch")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), before)
        app.state.store.close()

    def test_recovered_projection_preserves_initial_history_after_reopen(self):
        self.save()
        self.save("recovery")
        self.store.mark_patch_applied(self.did, "recovery")
        reopened = Store(self.store.path)
        value = reopened.get_runtime_patches(self.did)["local"]
        self.assertEqual(value["phase"], "recovery")
        self.assertTrue(value["applied"])
        self.assertEqual(value["initial"]["phase"], "initial")
        self.assertFalse(value["initial"]["applied"])
        self.assertFalse(reopened.save_validated_patch(self.did, self.candidate, self.mapping, self.receipt, "recovery"))
        self.assertTrue(reopened.get_runtime_patches(self.did)["local"]["applied"])
        reopened.close()

    def test_different_deployment_revision_cannot_publish(self):
        with self.store._lock, self.store._conn:
            self.store._conn.execute("UPDATE deployments SET commit_sha=? WHERE id=?", ("f" * 40, self.did))
        with self.assertRaises(ValueError):
            self.save()
        self.assertEqual(self.store.get_runtime_patches(self.did), {})

    def test_new_analysis_and_patch_reads_serialize_with_writes(self):
        self.save()
        errors = []
        def read():
            try:
                for _ in range(100):
                    self.store.get_runtime_patches(self.did)
                    self.store.get_deployment_analysis(self.did)
            except Exception as error:
                errors.append(type(error).__name__)
        threads = [threading.Thread(target=read) for _ in range(3)]
        for thread in threads:
            thread.start()
        for _ in range(100):
            self.store.add_event(self.did, {"step": "patch"})
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])

    def test_terminal_deployment_cannot_mark_unexecuted_patch_applied(self):
        self.save()
        self.store.fail(self.did)
        with self.assertRaises(ValueError):
            self.store.mark_patch_applied(self.did, "initial")
        self.assertFalse(self.store.get_runtime_patches(self.did)["local"]["applied"])


if __name__ == "__main__":
    unittest.main()
