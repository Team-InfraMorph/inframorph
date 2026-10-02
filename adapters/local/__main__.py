import argparse
import json
import sys
from pathlib import Path

from .runtime import deploy, rollback

p = argparse.ArgumentParser()
p.add_argument("action", choices=["deploy", "rollback"])
p.add_argument("--plan", type=Path)
p.add_argument("--artifact", type=Path)
p.add_argument("--state-dir", type=Path, required=True)
p.add_argument("--secrets-file", type=Path)
p.add_argument("--publish", action="store_true")
p.add_argument("--deployment-id", default="local-deploy")
a = p.parse_args()
if a.action == "deploy" and (a.plan is None or a.artifact is None):
    p.error("deploy requires --plan and --artifact")
sink = lambda e: print(e.jsonl(), end="", flush=True)
try:
    if a.action == "deploy":
        deploy(
            json.loads(a.plan.read_text()),
            json.loads(a.artifact.read_text()),
            a.state_dir,
            secret_values=json.loads(a.secrets_file.read_text())
            if a.secrets_file
            else None,
            publish=a.publish,
            deployment_id=a.deployment_id,
            sink=sink,
        )
    else:
        rollback(a.state_dir, deployment_id=a.deployment_id, sink=sink)
except Exception:
    print("local_operation_failed (see safe DeployEvent codes)", file=sys.stderr)
    sys.exit(1)
