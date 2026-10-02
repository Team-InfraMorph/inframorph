import json
import unittest
from pathlib import Path
from unittest.mock import patch

from policy_gate.gate import PolicyError
from scripts.verify_e_boundaries import verify_bindings


class BoundaryTests(unittest.TestCase):
    def inspect(self, service, ip):
        bindings = (
            {} if ip is None else {"3000/tcp": [{"HostIp": ip, "HostPort": "12345"}]}
        )
        return {
            "Config": {"Labels": {"com.docker.compose.service": service}},
            "HostConfig": {"PortBindings": bindings, "Privileged": False},
        }

    def check(self, containers):
        state = Path("/e-boundary-demo")
        with patch(
            "scripts.verify_e_boundaries.run",
            side_effect=["container-id", json.dumps(containers)],
        ):
            return verify_bindings(state, state / "run/compose.json")

    def test_actual_web_binding_must_be_loopback(self):
        with self.assertRaisesRegex(PolicyError, "web_not_loopback"):
            self.check([self.inspect("web", "0.0.0.0")])

    def test_private_services_cannot_publish_even_loopback(self):
        for service in ("db", "worker", "schema", "tunnel"):
            with (
                self.subTest(service=service),
                self.assertRaisesRegex(PolicyError, "private_service_published"),
            ):
                self.check([self.inspect(service, "127.0.0.1")])

    def test_expected_bindings_pass(self):
        self.assertEqual(
            self.check(
                [
                    self.inspect("web", "127.0.0.1"),
                    self.inspect("db", None),
                    self.inspect("worker", None),
                ]
            ),
            ["db", "web", "worker"],
        )
