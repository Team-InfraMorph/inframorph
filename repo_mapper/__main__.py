import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

from .mapper import map_snapshot
from .snapshot import git, snapshot_from_remote

URLS = {
    "https://github.com/Team-InfraMorph/demo-app",
    "https://github.com/Team-InfraMorph/demo-app.git",
}

def main():
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict) or set(request) != {
            "repo_url", "branch", "source_revision", "output_dir"
        }:
            raise ValueError("invalid_request")

        url = request["repo_url"]
        branch = request["branch"]
        if not isinstance(url, str) or url.rstrip("/") not in URLS:
            raise ValueError("unsupported_repository")
        if not isinstance(branch, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch
        ):
            raise ValueError("invalid_branch")

        git(Path.cwd(), "check-ref-format", "--branch", branch)

        revision = request["source_revision"]
        if revision is not None and (
            not isinstance(revision, str)
            or not re.fullmatch(r"[0-9a-f]{40}", revision)
        ):
            raise ValueError("invalid_revision")

        snapshot = snapshot_from_remote(
            url.rstrip("/"), branch, revision, request["output_dir"]
        )
        try:
            mapping = map_snapshot(snapshot, request["output_dir"])
        except Exception:
            shutil.rmtree(snapshot.path)
            raise

        print(json.dumps({
            "snapshot": "snapshot",
            "repo_map": mapping.model_dump(mode="json"),
        }))
    except (
        OSError,
        ValueError,
        TypeError,
        UnicodeError,
        subprocess.SubprocessError,
    ):
        print("repo_mapper_failed", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
