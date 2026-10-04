from schemas import Intent, Plan

from .pricing import estimate_aws_monthly_krw
from .database import make_db_plan


def make_plan(raw_intent, target):
    intent = Intent.model_validate(raw_intent)

    if target not in {"local", "aws", "gcp"} or intent.unknowns:
        raise ValueError("unresolved_or_unsupported_plan")
    if intent.app != "demo-app" or intent.runtime != "node22":
        raise ValueError("unsupported_app")

    names = {workload.name for workload in intent.workloads}
    if names not in ({"web"}, {"web", "worker"}):
        raise ValueError("unsupported_workloads")

    web = next(workload for workload in intent.workloads
               if workload.name == "web")
    if (
        web.kind.value,
        web.port,
        web.health,
        web.public,
        web.command,
    ) != ("http", 3000, "/health", True, None):
        raise ValueError("unsupported_web")

    if "worker" in names:
        worker = next(workload for workload in intent.workloads
                      if workload.name == "worker")
        if (
            worker.kind.value,
            worker.public,
            worker.command,
        ) != ("worker", False, "node src/worker.js"):
            raise ValueError("unsupported_worker")

    states = {item.kind: item for item in intent.state}
    if len(intent.state) != 2 or set(states) != {
        "relational_db", "persistent_files"
    }:
        raise ValueError("unsupported_state")

    db = states["relational_db"]
    storage = states["persistent_files"]

    if storage.path.rstrip("/") != "uploads":
        raise ValueError("unsupported_storage")

    db_plan = make_db_plan(db, target)

    if intent.secrets != ["DATABASE_URL"] or intent.config not in (
        {}, {"PORT": "3000"}
    ):
        raise ValueError("unsupported_environment")

    services = []
    for workload in intent.workloads:
        service = workload.model_dump(
            mode="json", exclude={"evidence"}
        )
        service.update(cpu=256, mem=512)
        services.append(service)

    config = dict(intent.config)
    config["STORAGE_DRIVER"] = {"local": "fs", "aws": "s3", "gcp": "gcs"}[target]

    mermaid = "flowchart LR\nweb --> db\nweb --> storage"
    if "worker" in names:
        mermaid += "\nworker --> db"

    return Plan.model_validate({
        "source_revision": intent.source_revision,
        "target": target,
        "app": intent.app,
        "image_tag": f"app:{intent.source_revision}",
        "services": services,

        "db": db_plan,

        "storage": {
            "type": {"local": "volume", "aws": "s3", "gcp": "gcs"}[target],
            "patch": "fs_to_storage",
            "path": storage.path,
        },
        "secrets": intent.secrets,
        "config": config,
        "logs": {"local": "docker", "aws": "cloudwatch", "gcp": "cloud_logging"}[target],
        "est_monthly_krw": (0 if target == "local" else
                            estimate_aws_monthly_krw(services) if target == "aws" else
                            estimate_gcp_monthly_krw(services)),
        "mermaid": mermaid,
    })
