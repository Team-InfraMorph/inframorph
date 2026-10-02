import json
import sys

from .engine import make_plan

def main():
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict) or set(request) != {
            "intent", "target"
        }:
            raise ValueError("invalid_request")

        plan = make_plan(request["intent"], request["target"])
        print(plan.model_dump_json())
    except (ValueError, TypeError):
        print("planner_failed", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())