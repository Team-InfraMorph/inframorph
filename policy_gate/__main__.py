import argparse
import json
import sys
from pathlib import Path

from .gate import PolicyError, validate_intent, validate_patch

p = argparse.ArgumentParser()
p.add_argument("kind", choices=["intent", "patch"])
p.add_argument("--snapshot", type=Path, required=True)
p.add_argument("--input", type=Path, required=True)
p.add_argument("--revision")
p.add_argument("--plan", type=Path)
a = p.parse_args()
if a.kind == "intent" and a.revision is None:
    p.error("intent requires --revision")
if a.kind == "patch" and a.plan is None:
    p.error("patch requires --plan")
try:
    if a.kind == "intent":
        validate_intent(json.loads(a.input.read_text()), a.snapshot, a.revision)
    else:
        validate_patch(a.snapshot, a.input, json.loads(a.plan.read_text()))
    print(json.dumps({"status": "ok", "stage": a.kind}))
except (PolicyError, OSError, ValueError, TypeError, AttributeError) as e:
    print(
        json.dumps(
            {
                "status": "fail",
                "code": str(e) if isinstance(e, PolicyError) else "invalid_input",
            }
        )
    )
    sys.exit(1)
