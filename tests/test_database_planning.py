import json
from pathlib import Path
import unittest

from pydantic import ValidationError

from adapters.aws.contracts import Plan as AwsPlan
from code_patch.runner import (
    PatchError,
    postgres_schema_candidate,
    transform,
)
from planner.database import make_db_plan
from planner.engine import make_plan
from policy_gate.gate import PolicyError
from policy_gate.rules import patch_rules, plan_rules
from policy_gate.structure import prisma_structure
from repo_mapper.rules import build_repo_map
from schemas import Intent, Plan
from schemas.plan import DbPlan

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = "prisma/schema.prisma"
BASE_SCHEMA = (
    ROOT / "tests/fixtures/analyzer/v1/snapshot" / SCHEMA_PATH
).read_bytes()

def schema_for(engine):
    return BASE_SCHEMA.replace(
        b'provider = "sqlite"',
        f'provider = "{engine}"'.encode(),
    )

def intent_for(engine):
    raw = json.loads(
        (ROOT / "schemas/fixtures/v1/intent.json").read_text()
    )
    database = next(
        item
        for item in raw["state"]
        if item["kind"] == "relational_db"
    )
    database["engine"] = engine
    return raw

def database_plan(engine):
    intent = Intent.model_validate(intent_for(engine))
    state = next(
        item for item in intent.state
        if item.kind == "relational_db"
    )
    return make_db_plan(state, "local")

def db_only_plan(engine):
    raw = make_plan(intent_for(engine), "local").model_dump(
        mode="json"
    )
    raw["storage"] = None
    raw["config"] = {}
    return Plan.model_validate(raw)

class DatabasePlanningTests(unittest.TestCase):
    def test_legacy_database_plan_round_trip(self):
        raw = {
            "type": "postgres_container",
            "patch": "sqlite_to_postgres",
        }

        checked = DbPlan.model_validate(raw)

        self.assertEqual(checked.source_engine, "sqlite")
        self.assertEqual(checked.target_engine, "postgresql")
        self.assertEqual(checked.orm, "prisma")
        self.assertEqual(
            checked.model_dump(mode="json"),
            raw,
        )

    def test_new_operation_requires_source_information(self):
        with self.assertRaises(ValidationError):
            DbPlan.model_validate({
                "type": "postgres_container",
                "patch": "prisma_to_postgres",
            })

    def test_legacy_operation_rejects_conflicting_source(self):
        with self.assertRaises(ValidationError):
            DbPlan.model_validate({
                "type": "postgres_container",
                "patch": "sqlite_to_postgres",
                "source_engine": "mysql",
                "orm": "prisma",
            })

    def test_mapper_and_planner_preserve_source_engine(self):
        engines = (
            "sqlite",
            "postgresql",
            "mysql",
            "sqlserver",
            "cockroachdb",
        )

        for engine in engines:
            with self.subTest(engine=engine):
                files = {
                    "package.json": (
                        b'{"scripts": {}, "dependencies": {}}'
                    ),
                    SCHEMA_PATH: schema_for(engine),
                }

                mapping = build_repo_map("a" * 40, files)
                self.assertEqual(mapping.db.provider, engine)

                for target, resource in (
                    ("local", "postgres_container"),
                    ("aws", "rds_postgres"),
                ):
                    plan = make_plan(intent_for(engine), target)

                    self.assertEqual(plan.schema_version, "1.0.0")
                    self.assertEqual(plan.db.source_engine, engine)
                    self.assertEqual(
                        plan.db.target_engine, "postgresql"
                    )
                    self.assertEqual(plan.db.type, resource)
                    expected_operation = {
                        "sqlite": "sqlite_to_postgres",
                        "postgresql": "none",
                    }.get(engine, "prisma_to_postgres")

                    self.assertEqual(
                        plan.db.patch,
                        expected_operation,
                    )

    def test_missing_engine_is_not_assumed_to_be_sqlite(self):
        raw = intent_for("mysql")
        database = next(
            item for item in raw["state"]
            if item["kind"] == "relational_db"
        )
        database.pop("engine")

        with self.assertRaises(ValidationError):
            make_plan(raw, "local")

    def test_inconsistent_patch_is_rejected(self):
        for engine, operation in (
            ("mysql", "none"),
            ("postgresql", "prisma_to_postgres"),
        ):
            with self.subTest(engine=engine):
                with self.assertRaises(ValidationError):
                    DbPlan(
                        type="postgres_container",
                        source_engine=engine,
                        target_engine="postgresql",
                        orm="prisma",
                        patch=operation,
                    )

    def test_invalid_target_has_correct_error(self):
        intent = Intent.model_validate(intent_for("sqlite"))
        state = next(
            item for item in intent.state
            if item.kind == "relational_db"
        )

        with self.assertRaisesRegex(
            ValueError, "unsupported_database_target"
        ):
            make_db_plan(state, "unsupported")

    def test_parser_returns_provider_structure_and_span(self):
        source = schema_for("mysql")
        provider, normalized, span = prisma_structure(source)

        self.assertEqual(provider, "mysql")
        self.assertEqual(len(span), 2)
        self.assertLess(span[0], span[1])

        _, postgres_structure, _ = prisma_structure(
            schema_for("postgresql")
        )
        self.assertEqual(normalized, postgres_structure)

    def test_candidate_preserves_comments_and_string_values(self):
        source = (
            b'// provider = "mysql"\n'
            + schema_for("mysql").replace(
                b"text String",
                b'text String @default("mysql")',
            )
        )
        expected = source.replace(
            b'    provider = "mysql"',
            b'    provider = "postgresql"',
            1,
        )

        result = postgres_schema_candidate(
            source, database_plan("mysql")
        )

        self.assertEqual(result, expected)
        self.assertEqual(
            postgres_schema_candidate(
                result, database_plan("mysql")
            ),
            result,
        )

    def test_candidate_rejects_wrong_source_engine(self):
        with self.assertRaisesRegex(
            PatchError, "database_source_mismatch"
        ):
            postgres_schema_candidate(
                schema_for("sqlite"),
                database_plan("mysql"),
            )

    def test_transform_calls_the_new_conversion_path(self):
        source = schema_for("mysql")
        original = {SCHEMA_PATH: source}

        result = transform(original, db_only_plan("mysql"))

        self.assertEqual(
            result[SCHEMA_PATH],
            schema_for("postgresql"),
        )
        self.assertEqual(original[SCHEMA_PATH], source)

    def test_transform_rejects_unreviewed_model_changes(self):
        source = schema_for("mysql").replace(
            b"text String",
            b"text String @unique",
        )

        with self.assertRaisesRegex(
            PatchError, "unsupported_prisma_schema"
        ):
            transform(
                {SCHEMA_PATH: source},
                db_only_plan("mysql"),
            )

    def test_existing_migrations_are_not_silently_reused(self):
        original = {
            SCHEMA_PATH: schema_for("mysql"),
            "prisma/migrations/001/migration.sql": b"SELECT 1;",
        }

        with self.assertRaisesRegex(
            PatchError, "existing_migrations_unsupported"
        ):
            transform(original, db_only_plan("mysql"))

    def test_plan_policy_checks_source_engine(self):
        intent = Intent.model_validate(intent_for("mysql"))
        raw = make_plan(intent_for("mysql"), "local").model_dump(
            mode="json"
        )
        raw["db"]["source_engine"] = "sqlite"
        wrong_plan = Plan.model_validate(raw)

        with self.assertRaisesRegex(
            PolicyError, "plan_state_mismatch"
        ):
            plan_rules(intent, wrong_plan)

    def test_patch_policy_rejects_unconverted_unchanged_schema(self):
        original = {SCHEMA_PATH: schema_for("mysql")}
        plan = db_only_plan("mysql")

        with self.assertRaisesRegex(
            PolicyError, "prisma_provider_invalid"
        ):
            patch_rules(
                original,
                dict(original),
                plan,
                changed=[],
                js={},
            )

    def test_aws_accepts_the_new_plan_contract(self):
        plan = make_plan(intent_for("mysql"), "aws")

        parsed = AwsPlan.parse(plan.model_dump(mode="json"))

        self.assertEqual(parsed.schema_version, "1.0.0")
        self.assertEqual(parsed.db.source_engine, "mysql")
        self.assertEqual(parsed.db.target_engine, "postgresql")

if __name__ == "__main__":
    unittest.main()