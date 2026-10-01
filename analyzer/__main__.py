"""Intent JSON on stdout; metrics or a safe error on stderr."""
import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import sys

from .backend import BackendError, OpenAIBackend, ReplayBackend
from .config import Limits
from .runner import AnalysisError, analyze


async def run(args) -> int:
    backend = None
    try:
        limits = Limits(timeout_seconds=args.timeout, max_estimated_usd=args.max_estimated_usd)
        with args.repo_map.open("rb") as source:
            data = source.read(limits.max_request_bytes + 1)
        if len(data) > limits.max_request_bytes:
            raise ValueError("repo_map_too_large")
        mapping = json.loads(data)
        backend = ReplayBackend.from_file(args.replay) if args.replay else OpenAIBackend()
        result = await analyze(mapping, args.snapshot, backend, limits)
        print(result.intent.model_dump_json(indent=2))
        print(json.dumps({"metrics": asdict(result.metrics)}), file=sys.stderr)
        return 0
    except AnalysisError as error:
        print(json.dumps(error.as_dict()), file=sys.stderr)
        return 1
    except BackendError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError):
        print(json.dumps({"error": "invalid_cli_input"}), file=sys.stderr)
        return 1
    finally:
        if isinstance(backend, OpenAIBackend):
            await backend.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-map", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--replay", type=Path, help="Offline response transcript; never calls OpenAI")
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--max-estimated-usd", type=float, default=1.0)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
