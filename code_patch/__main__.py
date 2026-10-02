import argparse
import json
from pathlib import Path

from .runner import PatchError, patch_snapshot


def main(argv=None):
    parser = argparse.ArgumentParser(description="Patch a snapshot copy; never run source or call APIs")
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--repo-map", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = patch_snapshot(args.snapshot, json.loads(args.repo_map.read_text()),
                                json.loads(args.plan.read_text()), args.output_dir)
    except (OSError, ValueError) as error:
        print(json.dumps({"error": str(error) if isinstance(error, PatchError) else "invalid_input"}))
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
