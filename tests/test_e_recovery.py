"""Failure behavior matters: reject before Docker and preserve last successful state."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adapters.local.runtime import (
    compose_document,
    deploy,
    private_file,
    request,
    rollback,
)
from builder.runtime import RuntimeFailure, image_lock
from policy_gate.gate import PolicyError, clean_env

ROOT = Path(__file__).resolve().parents[1]


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name).resolve() / "demo-app"
        self.state.mkdir()
        self.plan = json.loads(
            (ROOT / "schemas/fixtures/v1/plan.local.json").read_text()
        )
        self.artifact = {
            "source_revision": self.plan["source_revision"],
            "image": self.plan["image_tag"],
            "platform": "linux/amd64",
            "target": "local",
        }
        self.events, self.calls = [], []
        self.record = {
            "note": {"id": 1, "text": "dummy"},
            "image_url": "/api/images/test.png",
        }
        self.image_id = "sha256:" + "a" * 64

    def execute(self, args, **kwargs):
        self.calls.append(args)
        if args[1:3] == ["image", "inspect"]:
            return json.dumps(
                [
                    {
                        "Id": self.image_id,
                        "Os": "linux",
                        "Architecture": "amd64",
                        "Config": {
                            "Labels": {
                                "inframorph.source_revision": self.plan[
                                    "source_revision"
                                ],
                                "inframorph.patched_digest": "b" * 64,
                            }
                        },
                    }
                ]
            )
        if "port" in args:
            return "127.0.0.1:12345"
        return ""

    def establish(self):
        with (
            patch("adapters.local.runtime.request", return_value=b"{}"),
            patch("adapters.local.runtime.smoke", return_value=self.record),
        ):
            return deploy(
                self.plan,
                self.artifact,
                self.state,
                execute=self.execute,
                sink=self.events.append,
            )

    def test_success_records_immutable_image(self):
        result = self.establish()
        self.assertEqual(result["image_id"], self.image_id)
        self.assertEqual(
            json.loads((self.state / "current.json").read_text())["record"], self.record
        )
        self.assertEqual(self.events[-1].step, "url")

    def test_failed_upgrade_restores_and_checks_previous(self):
        previous = self.establish()
        with (
            patch("adapters.local.runtime.request", return_value=b"{}"),
            patch(
                "adapters.local.runtime.smoke",
                side_effect=[RuntimeFailure("smoke_failed"), self.record],
            ) as smoke,
        ):
            with self.assertRaisesRegex(RuntimeFailure, "smoke_failed"):
                deploy(
                    self.plan,
                    self.artifact,
                    self.state,
                    execute=self.execute,
                    sink=self.events.append,
                )
        self.assertEqual(
            json.loads((self.state / "current.json").read_text())["config"],
            previous["config"],
        )
        self.assertEqual(smoke.call_args.kwargs["record"], self.record)
        self.assertTrue(
            any(e.step == "rollback" and e.status == "ok" for e in self.events)
        )

    def test_first_failure_never_creates_success_record(self):
        with (
            patch("adapters.local.runtime.request", return_value=b"{}"),
            patch(
                "adapters.local.runtime.smoke",
                side_effect=RuntimeFailure("smoke_failed"),
            ),
        ):
            with self.assertRaises(RuntimeFailure):
                deploy(self.plan, self.artifact, self.state, execute=self.execute)
        self.assertFalse((self.state / "current.json").exists())
        cleanup = [args for args in self.calls if "down" in args]
        self.assertEqual(len(cleanup), 1)
        self.assertNotIn("-v", cleanup[0])
        self.assertNotIn("--volumes", cleanup[0])

    def test_rollback_validates_both_persistence_records(self):
        self.establish()
        self.establish()
        with patch("adapters.local.runtime.smoke", return_value=self.record) as smoke:
            result = rollback(self.state, execute=self.execute, sink=self.events.append)
        self.assertEqual(smoke.call_count, 2)
        self.assertIsNone(result["public_url"])
        self.assertEqual(self.events[-1].status, "ok")

    def test_config_cannot_override_secret_or_interpolate_host(self):
        for key, value in [("DATABASE_URL", "fake"), ("CUSTOM", "${HOME}")]:
            plan = dict(self.plan, config={**self.plan["config"], key: value})
            with self.assertRaises(PolicyError):
                compose_document(plan, self.image_id, "inframorph-demo-app")

    def test_existing_state_permissions_tightened(self):
        file = self.state / "secret"
        file.write_text("dummy")
        file.chmod(0o644)
        private_file(file, "new")
        self.assertEqual(file.stat().st_mode & 0o777, 0o600)

    def test_same_image_wait_can_be_disabled_with_zero_timeout(self):
        with image_lock(self.artifact["image"]):
            with self.assertRaisesRegex(RuntimeFailure, "image_build_wait_timeout"):
                with image_lock(self.artifact["image"], timeout=0):
                    pass

    def test_network_disconnect_is_retryable(self):
        import http.client

        with patch(
            "urllib.request.urlopen", side_effect=http.client.RemoteDisconnected()
        ):
            with self.assertRaisesRegex(RuntimeFailure, "smoke_http_failed"):
                request("http://127.0.0.1:1")

    def test_environment_keeps_docker_user_but_drops_secrets(self):
        with patch.dict(
            "os.environ",
            {
                "USER": "test",
                "AWS_SECRET_ACCESS_KEY": "dummy",
                "OPENAI_API_KEY": "dummy",
            },
        ):
            env = clean_env()
        self.assertEqual(env["USER"], "test")
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertNotIn("OPENAI_API_KEY", env)

    def test_publish_tunnel_targets_only_internal_web(self):
        document = compose_document(
            self.plan, self.image_id, "inframorph-demo-app", publish=True
        )
        self.assertIn("http://web:3000", document["services"]["tunnel"]["command"])
        self.assertNotIn("ports", document["services"]["tunnel"])

    def test_first_public_failure_cleans_tunnel(self):
        with (
            patch("adapters.local.runtime.request", return_value=b"{}"),
            patch(
                "adapters.local.runtime.smoke",
                side_effect=RuntimeFailure("smoke_failed"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeFailure, "smoke_failed"):
                deploy(
                    self.plan,
                    self.artifact,
                    self.state,
                    publish=True,
                    execute=self.execute,
                    sink=self.events.append,
                )
        self.assertTrue(any("down" in args for args in self.calls))
        self.assertEqual(self.events[-1].detail, "initial_deployment_cleaned")

    def test_first_failure_cleanup_error_is_reported(self):
        def execute(args, **kwargs):
            if "down" in args:
                raise RuntimeFailure("command_failed")
            return self.execute(args, **kwargs)

        with (
            patch("adapters.local.runtime.request", return_value=b"{}"),
            patch(
                "adapters.local.runtime.smoke",
                side_effect=RuntimeFailure("smoke_failed"),
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeFailure, "initial_deployment_cleanup_failed"
            ):
                deploy(
                    self.plan,
                    self.artifact,
                    self.state,
                    publish=True,
                    execute=execute,
                    sink=self.events.append,
                )
        self.assertEqual(self.events[-1].status, "fail")
        self.assertEqual(self.events[-1].detail, "initial_deployment_cleanup_failed")
