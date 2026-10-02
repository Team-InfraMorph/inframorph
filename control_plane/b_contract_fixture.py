"""Explicit command stand-ins for contract tests; not B Mapper/Planner code."""
import argparse
import json
from pathlib import Path
import sys

from schemas import Intent
from .b_bridge import DemoModules, LIMIT


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("module", choices=("mapper", "planner"))
    args = parser.parse_args(argv)
    request = json.loads(sys.stdin.buffer.read(LIMIT + 1))
    modules = DemoModules()
    if args.module == "mapper":
        source = modules.map({"repo_url": request["repo_url"], "branch": request["branch"]},
            {"commit_sha": request["source_revision"]}, Path(request["output_dir"]))
        reply = {"snapshot": "snapshot", "repo_map": source.repo_map.model_dump(mode="json")}
    else:
        if request["target"] != "local":
            raise ValueError("local_contract_only")
        reply = modules.plan(Intent.model_validate(request["intent"])).model_dump(mode="json")
    print(json.dumps(reply))


if __name__ == "__main__":
    main()
