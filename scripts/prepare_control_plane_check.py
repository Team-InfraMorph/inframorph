"""Assemble trusted D Git code + current C code in a NEW local check directory.

No checkout changes, fetch, package installs, API requests or Docker execution.
Only use a reviewed team ref: the resulting checkout will execute its code.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import uuid

ROOT = Path(__file__).resolve().parents[1]


def prepare(team_ref, output, runtime_ref=None):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", team_ref):
        raise ValueError("invalid_team_ref")
    commit = subprocess.check_output(["git", "rev-parse", "--verify", team_ref + "^{commit}"],
                                     cwd=ROOT, stderr=subprocess.DEVNULL, text=True).strip()
    data = subprocess.check_output(["git", "archive", "--format=tar", commit], cwd=ROOT)
    output = Path(output).resolve()
    if any(folder == output or folder in output.parents for folder in (ROOT / "analyzer", ROOT / "code_patch", ROOT / "tests")):
        raise ValueError("output_overlaps_source")
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    try:
        source = output / "source"
        source.mkdir()
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            archive.extractall(source, filter="data")
        if not (source / "control_plane/analysis.py").is_file():
            raise ValueError("control_plane_unavailable")
        runtime_commit = None
        if runtime_ref is not None:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", runtime_ref):
                raise ValueError("invalid_runtime_ref")
            runtime_commit = subprocess.check_output(["git", "rev-parse", "--verify", runtime_ref + "^{commit}"],
                                                     cwd=ROOT, stderr=subprocess.DEVNULL, text=True).strip()
            runtime_data = subprocess.check_output(["git", "archive", "--format=tar", runtime_commit,
                                                    "policy_gate", "builder", "adapters/local", "tests", "scripts"], cwd=ROOT)
            with tarfile.open(fileobj=io.BytesIO(runtime_data)) as archive:
                archive.extractall(source, filter="data")
        for folder in ("analyzer", "code_patch", "tests", "scripts"):
            shutil.copytree(ROOT / folder, source / folder, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__"))
        own = (ROOT / "requirements.txt").read_text().splitlines()
        team = (source / "requirements.txt").read_text().splitlines()
        (source / "requirements.txt").write_text("\n".join(dict.fromkeys(own + team)) + "\n")
        manifest = {"team_ref": team_ref, "team_commit": commit, "source": str(source),
                    "runtime_ref": runtime_ref, "runtime_commit": runtime_commit,
                    "scope": "trusted D/E refs plus current C modules/tests; original checkout untouched",
                    "implementation_sha256": {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
                        for folder in (source / "analyzer", source / "control_plane", source / "code_patch",
                                       source / "policy_gate", source / "builder", source / "adapters/local")
                        for p in sorted(folder.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}}
        (output / "source-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        return manifest
    except Exception:
        # Only this call's fresh directory; never remove an existing checkout.
        shutil.rmtree(output)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--team-ref", default="origin/feat/d-control-plane")
    parser.add_argument("--runtime-ref", help="Optional reviewed E Git ref for actual runtime integration")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    output = args.output_dir or ROOT / ".local/control-plane-check" / uuid.uuid4().hex[:12]
    try:
        manifest = prepare(args.team_ref, output, args.runtime_ref)
    except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError):
        print(json.dumps({"error": "control_plane_check_preparation_failed"}), file=sys.stderr)
        return 1
    print(json.dumps({"source": manifest["source"], "team_commit": manifest["team_commit"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
