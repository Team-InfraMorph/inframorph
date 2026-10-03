import unittest

from pydantic import ValidationError

from schemas import BuildArtifact, DeployEvent, Intent, Plan


SHA = "a" * 40


def intent_data():
    return {
        "source_revision": SHA,
        "app": "demo-app",
        "runtime": "node22",
        "workloads": [{
            "name": "web",
            "kind": "http",
            "port": 3000,
            "public": True,
            "health": "/health",
            "evidence": ["src/server.js:10"],
        }],
        "state": [{
            "kind": "relational_db",
            "engine": "sqlite",
            "orm": "prisma",
            "evidence": ["prisma/schema.prisma:6"],
        }],
        "secrets": ["DATABASE_URL"],
        "unknowns": [],
    }


def plan_data():
    return {
        "source_revision": SHA,
        "target": "aws",
        "app": "demo-app",
        "image_tag": f"app:{SHA}",
        "services": [{
            "name": "web",
            "kind": "http",
            "cpu": 256,
            "mem": 512,
            "port": 3000,
            "health": "/health",
            "public": True,
        }],
        "db": {"type": "rds_postgres", "patch": "sqlite_to_postgres"},
        "storage": {
            "type": "s3",
            "patch": "fs_to_storage",
            "path": "uploads/",
        },
        "secrets": ["DATABASE_URL"],
        "config": {"STORAGE_DRIVER": "s3"},
        "logs": "cloudwatch",
        "est_monthly_krw": None,
        "mermaid": "flowchart LR\nweb --> db\nweb --> storage",
    }


class ContractTests(unittest.TestCase):
    def test_valid_intent_and_plan(self):
        Intent.model_validate(intent_data())
        Plan.model_validate(plan_data())

    def test_valid_gcp_plan(self):
        data = plan_data()
        data.update(target="gcp", logs="cloud_logging")
        data["db"]["type"] = "cloudsql_postgres"
        data["storage"]["type"] = "gcs"
        data["config"]["STORAGE_DRIVER"] = "gcs"
        Plan.model_validate(data)

    def test_wrong_version_is_rejected(self):
        data = intent_data()
        data["schema_version"] = "2.0.0"
        with self.assertRaises(ValidationError):
            Intent.model_validate(data)

    def test_http_requires_health(self):
        data = intent_data()
        data["workloads"][0].pop("health")
        with self.assertRaises(ValidationError):
            Intent.model_validate(data)

    def test_state_requires_engine(self):
        data = intent_data()
        data["state"][0].pop("engine")
        with self.assertRaises(ValidationError):
            Intent.model_validate(data)

    def test_local_cannot_claim_rds(self):
        data = plan_data()
        data["target"] = "local"
        data["logs"] = "docker"
        data["storage"]["type"] = "volume"
        data["config"]["STORAGE_DRIVER"] = "fs"
        with self.assertRaises(ValidationError):
            Plan.model_validate(data)

    def test_image_must_match_revision(self):
        data = plan_data()
        data["image_tag"] = "app:wrong"
        with self.assertRaises(ValidationError):
            Plan.model_validate(data)

    def test_naive_event_time_is_rejected(self):
        with self.assertRaises(ValidationError):
            DeployEvent.model_validate({
                "deployment_id": "example",
                "ts": "2026-10-01T12:00:00",
                "target": "aws",
                "step": "build",
                "status": "ok",
            })

    def test_builder_image_must_match_revision(self):
        with self.assertRaises(ValidationError):
            BuildArtifact.model_validate({
                "source_revision": SHA,
                "target": "aws",
                "image": "app:wrong",
                "platform": "linux/amd64",
            })


if __name__ == "__main__":
    unittest.main()
