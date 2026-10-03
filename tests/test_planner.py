import json
from pathlib import Path
import unittest

from planner.engine import make_plan
from planner.pricing import estimate_aws_monthly_krw

ROOT = Path(__file__).resolve().parents[1]


class PlannerTests(unittest.TestCase):
    def test_v1_v2_local_aws_contracts(self):
        for version in ("v1", "v2"):
            fixture = ROOT / "schemas" / "fixtures" / version
            intent = json.loads((fixture / "intent.json").read_text())
            for target in ("local", "aws"):
                with self.subTest(version=version, target=target):
                    expected = json.loads((fixture / f"plan.{target}.json").read_text())
                    actual = make_plan(intent, target).model_dump(mode="json")
                    self.assertEqual(actual, expected)

    def test_unresolved_and_unsupported_intents_fail(self):
        fixture = ROOT / "schemas" / "fixtures" / "v1" / "intent.json"
        intent = json.loads(fixture.read_text())
        cases = [
            ({"unknowns": ["database ownership unclear"]}, "aws"),
            ({"runtime": "python"}, "aws"),
            ({"secrets": ["DATABASE_URL", "API_KEY"]}, "aws"),
            ({}, "unsupported_target"),
        ]
        for changes, target in cases:
            with self.subTest(changes=changes, target=target):
                with self.assertRaises(ValueError):
                    make_plan({**intent, **changes}, target)

    def test_price_is_explicit_deterministic_estimate(self):
        services = [{"cpu": 256, "mem": 512}]
        self.assertEqual(estimate_aws_monthly_krw(services), 254716)
        self.assertEqual(
            estimate_aws_monthly_krw(services * 2), 271296
        )


if __name__ == "__main__":
    unittest.main()
