import argparse
import json
import sys
from pathlib import Path

from .runtime import build

p = argparse.ArgumentParser()
p.add_argument("--snapshot", type=Path, required=True)
p.add_argument("--bundle", type=Path, required=True)
p.add_argument("--plan", type=Path, required=True)
p.add_argument("--output", type=Path, required=True)
p.add_argument("--deployment-id", default="local-build")
a = p.parse_args()
try:
    artifact = build(
        a.snapshot,
        a.bundle,
        json.loads(a.plan.read_text()),
        deployment_id=a.deployment_id,
        sink=lambda e: print(e.jsonl(), end="", flush=True),
    )
    a.output.write_text(artifact.model_dump_json(indent=2) + "\n")
except Exception:
    print("build_failed (see safe DeployEvent codes)", file=sys.stderr)
    sys.exit(1)
