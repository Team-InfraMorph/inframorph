"""Independent deployment authorization for the already-reviewed demo profile.

This is deliberately not an arbitrary-JavaScript semantic checker or an injection
classifier. Documentation/metadata cannot change operational fields. Unreviewed
runtime code (including added comments) requires review before it can be executed.
"""
import hashlib
import json
from pathlib import Path

from schemas import Intent, Plan
from policy_gate.reporting import checked, rule
from schemas.common import parse_evidence
from code_patch.runner import read_snapshot


POLICY_CODES = frozenset({"unreviewed_runtime_source", "intent_source_mismatch",
                         "non_source_evidence", "invalid_source_evidence", "plan_source_mismatch"})
POLICY_FIELDS = ("source_revision", "app", "unknowns", "workloads", "state", "secrets", "config")


class SourcePolicyError(ValueError):
    """Public diagnostics contain only host-owned codes and field names."""
    def __init__(self, code, fields=()):
        self.code = code if code in POLICY_CODES else "local_pipeline_analysis_failed"
        self.fields = tuple(name for name in POLICY_FIELDS if name in fields)
        super().__init__(self.code)


@checked('source')
def validate_demo_intent(value, source, mapping):
    with rule('G-002') as evidence:
        intent = Intent.model_validate(value.model_dump() if isinstance(value, Intent) else value)
        files, _ = read_snapshot(Path(source), mapping)
        profile = json.loads(Path(__file__).with_name("demo-profile.json").read_text())
        evidence.update(source_revision=mapping.commit,file_count=len(files))
        worker = "src/worker.js" in files
        required = dict(profile["shared"])
        required["src/server.js"] = profile["server"]
        if worker:
            required["src/worker.js"] = [profile["worker"]]
        for name, hashes in required.items():
            if name not in files or hashlib.sha256(files[name]).hexdigest() not in hashes:
                raise SourcePolicyError("unreviewed_runtime_source")
        # Every executable/build input must be reviewed, even if the model never reads it.
        allowed = set(required) | {"package.json", "package-lock.json"}
        for name in files:
            suffix = Path(name).suffix.lower()
            if name not in allowed and (name.startswith(("src/", "prisma/")) or
                                        suffix not in {".md", ".txt", ".rst"}):
                raise SourcePolicyError("unreviewed_runtime_source")
        package = json.loads(files.get("package.json", b"{}"))
        if not isinstance(package, dict):
            raise SourcePolicyError("unreviewed_runtime_source")
        scripts = package.get("scripts", {})
        expected_scripts = {"start": "node src/server.js"}
        if worker:
            expected_scripts["worker"] = "node src/worker.js"
        if (package.get("name") != "demo-app" or not isinstance(scripts, dict) or
                {k: v for k, v in scripts.items() if k != "test"} != expected_scripts or
                package.get("dependencies") != profile["dependencies"] or
                package.get("devDependencies") != {"prisma": "6.19.3"}):
            raise SourcePolicyError("unreviewed_runtime_source")
        workloads = [("web", "http", 3000, "/health", True, None)]
        if worker:
            workloads.append(("worker", "worker", None, None, False, "node src/worker.js"))
        actual = [(w.name, w.kind.value, w.port, w.health, w.public, w.command) for w in intent.workloads]
        states = sorted((s.kind, s.engine, s.orm, s.path.rstrip("/") if s.path else None) for s in intent.state)
        expected_states = sorted([("relational_db", "sqlite", "prisma", None),
                                  ("persistent_files", None, None, "uploads")])
        mismatches = {
            "source_revision": intent.source_revision != mapping.commit,
            "app": intent.app != package["name"],
            "unknowns": bool(intent.unknowns),
            "workloads": sorted(actual) != sorted(workloads),
            "state": states != expected_states,
            "secrets": intent.secrets != ["DATABASE_URL"],
            "config": intent.config not in ({}, {"PORT": "3000"}),
        }
        if any(mismatches.values()):
            raise SourcePolicyError("intent_source_mismatch", [name for name, failed in mismatches.items() if failed])
        for entity in [*intent.workloads, *intent.state]:
            for citation in entity.evidence:
                name, line = parse_evidence(citation)
                if name not in set(required) | {"package.json"}:
                    raise SourcePolicyError("non_source_evidence")
                rows = files[name].decode().splitlines()
                if line > len(rows) or not rows[line - 1].strip():
                    raise SourcePolicyError("invalid_source_evidence")
        return intent


@checked('profile')
def validate_demo_plan(value, mapping, *, target="local"):
    with rule('G-003') as evidence:
        """B output is data too; allow only reviewed demo execution settings."""
        plan = Plan.model_validate(value.model_dump() if isinstance(value, Plan) else value)
        evidence.update(source_revision=mapping.commit,target=target,services=[s.name for s in plan.services],public_services=[s.name for s in plan.services if s.public])
        expected = [("web", "http", 3000, "/health", True, None)]
        if "src/worker.js" in mapping.tree:
            expected.append(("worker", "worker", None, None, False, "node src/worker.js"))
        actual = [(s.name, s.kind.value, s.port, s.health, s.public, s.command) for s in plan.services]
        config = dict(plan.config)
        config.pop("PORT", None)
        if target not in {"local", "aws", "gcp"}:
            raise SourcePolicyError("plan_source_mismatch")
        storage_driver = {"local": "fs", "aws": "s3", "gcp": "gcs"}[target]
        if (plan.source_revision != mapping.commit or plan.app != "demo-app" or plan.target.value != target or
                sorted(actual) != sorted(expected) or plan.db is None or plan.storage is None or
                plan.storage.path.rstrip("/") != "uploads" or plan.secrets != ["DATABASE_URL"] or
                config != {"STORAGE_DRIVER": storage_driver} or
                plan.config.get("PORT", "3000") != "3000" or
                (target in {"aws", "gcp"} and any(s.cpu != 256 or s.mem != 512 for s in plan.services))):
            raise SourcePolicyError("plan_source_mismatch")
        return plan
