"""Development-only integration test for the fixed, reviewed demo fixtures.

Requires Docker and Node. Never invokes a model, reads .env, or deploys to AWS.
Not a production Builder, Local Adapter or Policy Gate for arbitrary repos.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid

from schemas import Plan, RepoMap
from .runner import ALLOWED_PATHS, TEMPLATES, patch_snapshot, tree_digest


ROOT = Path(__file__).resolve().parents[1]
# A real 1x1 PNG; assertions compare the exact uploaded bytes.
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010804000000b51c0c02"
                    "0000000b4944415478da6364f80f00010501012718e3660000000049454e44ae426082")


class SmokeError(RuntimeError):
    pass


def environment():
    # Docker credentials remain in its normal local config; app/API credentials
    # and shell/environment overrides are not forwarded to child processes.
    return {k: v for k, v in os.environ.items() if k in {"HOME", "PATH", "TMPDIR", "LANG"}}


def command(args, *, timeout=60, cwd=None):
    result = subprocess.run(args, cwd=cwd, env=environment(), capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Do not print raw application output or commands containing env values.
        raise SmokeError("command_failed:" + args[0] + ":" + args[1])
    return result.stdout.strip()


def docker(*args, timeout=60):
    return command(["docker", *args], timeout=timeout)


def wait_for(check, *, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except (SmokeError, OSError, ValueError):
            pass
        time.sleep(0.5)
    raise SmokeError("readiness_timeout")


def request(url, *, data=None, content_type=None, expected=200):
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": content_type} if content_type else {})
    # Never inherit a proxy pointing outside this localhost test.
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = client.open(req, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        if response.status != expected:
            raise SmokeError(f"unexpected_http_status:expected_{expected}:actual_{response.status}")
        return response.read()


def preflight(source, original, report):
    """Limited fixed-fixture checks, not E's Policy Gate approval."""
    files = {p.relative_to(source).as_posix(): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    before = {p.relative_to(original).as_posix(): p.read_bytes() for p in original.rglob("*") if p.is_file()}
    changed = {name for name in files.keys() | before.keys() if files.get(name) != before.get(name)}
    if (not changed <= ALLOWED_PATHS or tree_digest(files) != report["patched_digest"] or
            tree_digest(before) != report["original_digest"]):
        raise SmokeError("preflight_digest_or_allowlist_failure")
    if hashlib.sha256((source.parent / "patch.diff").read_bytes()).hexdigest() != report["diff_sha256"]:
        raise SmokeError("preflight_diff_digest_failure")
    for name in files:
        if name.endswith(".js"):
            command(["node", "--check", str(source / name)])


def run_case(case, output):
    output.mkdir()
    prefix = "inframorph-smoke-" + uuid.uuid4().hex[:12]
    image = prefix + ":test"
    created = {"containers": [], "volumes": [], "networks": []}
    report = {"case": case, "status": "error", "checks": [], "team_api_called": False,
              "aws_called": False, "scope": "fixed demo fixtures with local Docker; no production Policy Gate"}

    def passed(name):
        report["checks"].append(name)
        print(json.dumps({"case": case, "passed": name}), flush=True)

    try:
        fixture = ROOT / "tests/fixtures/analyzer" / case
        original = output / "original"
        shutil.copytree(fixture / "snapshot", original)
        # Include the exact public demo lockfile; Analyzer's smaller fixture does not.
        shutil.copyfile(TEMPLATES / "base-package-lock.json", original / "package-lock.json")
        mapping = RepoMap.model_validate_json((fixture / "repo_map.json").read_text())
        mapping.tree.append("package-lock.json")
        plan = Plan.model_validate_json((ROOT / "schemas/fixtures" / case / "plan.local.json").read_text())
        patch = patch_snapshot(original, mapping, plan, output / "local-patch")
        aws = Plan.model_validate_json((ROOT / "schemas/fixtures" / case / "plan.aws.json").read_text())
        cloud_patch = patch_snapshot(original, mapping, aws, output / "aws-patch")
        if patch["patched_digest"] != cloud_patch["patched_digest"]:
            raise SmokeError("target_code_diverged")
        report.update(source_revision=mapping.commit, patched_digest=patch["patched_digest"],
                      plan_image_tag=plan.image_tag)
        passed("local_aws_identical_patched_source")
        source = output / "local-patch/source"
        preflight(source, original, patch)
        passed("copy_immutable_allowlist_digests_js_syntax")
        print(json.dumps({"case": case, "step": "building_node22_image"}), flush=True)
        docker("build", "--platform", "linux/amd64", "--label", "inframorph.smoke=" + prefix,
               "-f", str(TEMPLATES / "Dockerfile.smoke"), "-t", image, str(source), timeout=600)
        report["image_id"] = docker("image", "inspect", image, "--format", "{{.Id}}")
        report["platform"] = docker("image", "inspect", image, "--format", "{{.Os}}/{{.Architecture}}")
        passed("npm_ci_prisma_validate_generate_image_build")
        # Real installed SDK talks only to an in-container loopback HTTP stub.
        sdk_test = prefix + "-sdk"
        created["containers"].append(sdk_test)
        docker("run", "--rm", "--name", sdk_test, "--network", "none", "--read-only",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--mount", "type=bind,src=" + str(ROOT / "tests/test_storage_sdk.cjs") + ",dst=/app/test_storage_sdk.cjs,readonly",
               image, "node", "--test", "test_storage_sdk.cjs")
        passed("real_s3_sdk_loopback_stub_without_external_network")

        # A normal dedicated bridge is needed for Docker Desktop's localhost
        # port forwarding. An --internal bridge does not publish that port.
        docker("network", "create", "--label", "inframorph.smoke=" + prefix, prefix)
        created["networks"].append(prefix)
        for suffix in ("db", "uploads"):
            name = prefix + "-" + suffix
            docker("volume", "create", "--label", "inframorph.smoke=" + prefix, name)
            created["volumes"].append(name)
        db = prefix + "-db"
        created["containers"].append(db)
        # Trust auth is restricted to this temporary test network. PostgreSQL
        # has no published host port. No cloud credentials enter the containers.
        docker("run", "-d", "--name", db, "--network", prefix, "--network-alias", "db",
               "--label", "inframorph.smoke=" + prefix, "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
               "-e", "POSTGRES_USER=inframorph", "-e", "POSTGRES_DB=inframorph",
               "-v", db + ":/var/lib/postgresql/data", "postgres:16-alpine")
        wait_for(lambda: "accepting connections" in docker("exec", db, "pg_isready", "-U", "inframorph"))
        db_env = "DATABASE_URL=postgresql://inframorph@db:5432/inframorph"
        migration = prefix + "-schema"
        created["containers"].append(migration)
        docker("run", "--rm", "--name", migration, "--network", prefix, "-e", db_env,
               image, "./node_modules/.bin/prisma", "db", "push", "--skip-generate", timeout=90)
        passed("postgres_schema_created_without_data_loss_flag")

        web = prefix + "-web"
        worker = prefix + "-worker"
        common = ["--network", prefix, "--label", "inframorph.smoke=" + prefix, "--read-only",
                  "--tmpfs", "/tmp", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                  "--memory", "512m", "--cpus", "1", "-e", db_env, "-e", "STORAGE_DRIVER=fs", "-e", "PORT=3000"]

        def start_web():
            if web not in created["containers"]:
                created["containers"].append(web)
            docker("run", "-d", "--name", web, *common, "-p", "127.0.0.1::3000",
                   "-v", prefix + "-uploads:/app/uploads", image)
            address = docker("port", web, "3000/tcp")
            if not address.startswith("127.0.0.1:"):
                raise SmokeError("unexpected_published_address")
            url = "http://" + address
            wait_for(lambda: json.loads(request(url + "/health")).get("status") == "ok")
            return url

        base = start_web()
        passed("http_health_uses_postgres")
        note = json.loads(request(base + "/api/notes", data=b'{"text":"InfraMorph local persistence"}',
                                  content_type="application/json", expected=201))
        if note not in json.loads(request(base + "/api/notes")):
            raise SmokeError("note_readback_failed")
        uploaded = json.loads(request(base + "/api/images", data=PNG, content_type="image/png", expected=201))
        if request(base + uploaded["url"]) != PNG:
            raise SmokeError("image_readback_failed")
        passed("notes_and_png_write_read")
        request(base + "/api/notes", data=b'{"text":""}', content_type="application/json", expected=400)
        request(base + "/api/images", data=b"bad", content_type="text/plain", expected=415)
        request(base + "/api/images/invalid.png", expected=404)
        request(base + "/api/images/00000000-0000-0000-0000-000000000000.png", expected=404)
        passed("invalid_inputs_and_missing_images")

        # Recreate the app container, and restart PostgreSQL using its same volume.
        docker("rm", "-f", web)
        docker("restart", db)
        wait_for(lambda: "accepting connections" in docker("exec", db, "pg_isready", "-U", "inframorph"))
        base = start_web()
        if note not in json.loads(request(base + "/api/notes")) or request(base + uploaded["url"]) != PNG:
            raise SmokeError("restart_persistence_failed")
        passed("app_recreation_and_db_restart_preserve_notes_images")
        if any(service.kind.value == "worker" for service in plan.services):
            created["containers"].append(worker)
            docker("run", "-d", "--name", worker, *common, image, "node", "src/worker.js")

            def worker_observed_note():
                rows = docker("logs", worker).splitlines()
                for row in rows:
                    try:
                        event = json.loads(row)
                    except ValueError:
                        continue
                    if event.get("event") == "note_count" and event.get("count") == 1:
                        return True
                return False

            wait_for(worker_observed_note, timeout=30)
            if docker("inspect", worker, "--format", "{{.Image}}") != report["image_id"]:
                raise SmokeError("worker_image_differs")
            docker("stop", "--time", "15", worker)
            if docker("inspect", worker, "--format", "{{.State.ExitCode}}") != "0":
                raise SmokeError("worker_shutdown_failed")
            passed("worker_same_image_reads_postgres_and_stops_cleanly")
        report["status"] = "passed"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        report["error"] = str(error) if isinstance(error, SmokeError) else type(error).__name__
    finally:
        # Remove only resources generated by this run. Never run Docker prune.
        cleanup_errors = []
        for names, operation in ((created["containers"], ["rm", "-f"]),
                                 (created["volumes"], ["volume", "rm"]),
                                 (created["networks"], ["network", "rm"])):
            for name in reversed(names):
                try:
                    result = subprocess.run(["docker", *operation, name], env=environment(), capture_output=True, timeout=30)
                    failed = result.returncode and b"No such" not in result.stderr
                except (OSError, subprocess.SubprocessError):
                    failed = True
                if failed:
                    cleanup_errors.append(name)
        # Preserve the uniquely named image for investigation/reuse, and record it.
        report["local_image"] = image
        report["cleanup_errors"] = cleanup_errors
        if cleanup_errors:
            report["status"] = "error"
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("v1", "v2", "all"), default="all")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.output_dir or ROOT / ".local/code-patch-smoke" / (stamp + "-" + uuid.uuid4().hex[:6])
    output = output.absolute()
    output.mkdir(parents=True, exist_ok=False)
    reports = [run_case(case, output / case) for case in (("v1", "v2") if args.case == "all" else (args.case,))]
    summary = {"results": reports, "output_dir": str(output), "team_api_called": False, "aws_called": False}
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary), flush=True)
    return 0 if all(report["status"] == "passed" for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
