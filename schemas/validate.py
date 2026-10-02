import argparse
import sys
from pathlib import Path
from pydantic import ValidationError

from . import BuildArtifact, DeployEvent, Intent, Plan, RepoMap

MODELS = {
    "repo_map": RepoMap,
    "intent": Intent,
    "plan": Plan,
    "build_artifact": BuildArtifact,
}

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=[*MODELS, "events"])
    parser.add_argument("path", type=Path)
    args = parser.parse_args()

    try:
        contents = args.path.read_text(encoding="utf-8")
        if args.kind == "events":
            lines = [line for line in contents.splitlines() if line.strip()]
            if not lines:
                raise ValueError("이벤트 파일이 비어 있습니다")
            for number, line in enumerate(lines, 1):
                try:
                    DeployEvent.model_validate_json(line)
                except ValidationError as error:
                    raise ValueError(f"{number}번째 이벤트: {error}") from error
        else:
            MODELS[args.kind].model_validate_json(contents)
    except (OSError, ValidationError, ValueError) as error:
        print(f"INVALID {args.path}: {error}", file=sys.stderr)
        return 1

    print(f"VALID {args.path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())