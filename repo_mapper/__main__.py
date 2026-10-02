"""Control-plane command: one JSON request on stdin, one JSON reply on stdout.

Reply: {"snapshot": "snapshot", "repo_map": RepoMap}. Failures exit 1 and write
only a fixed error code to stderr.
"""
import json
import sys

from . import MapperError, map_repository


LIMIT = 120_000


def unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate_json_key")
        result[name] = value
    return result


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise MapperError("invalid_request")
        try:
            request = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
        except (ValueError, UnicodeError, RecursionError):
            raise MapperError("invalid_request") from None
        mapped = map_repository(request)
    except MapperError as error:
        print(error.code, file=sys.stderr)
        return 1
    except Exception:
        print("mapper_failed", file=sys.stderr)
        return 1
    print(json.dumps({"snapshot": mapped.snapshot.name, "repo_map": mapped.repo_map.model_dump(mode="json")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
