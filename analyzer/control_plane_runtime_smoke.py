"""Opt-in D API + actual E Docker integration; fixture B and model replay only."""
import argparse
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from fastapi.testclient import TestClient
from control_plane.app import create_app
from control_plane.b_bridge import DemoModules
from control_plane.runtime import LocalRuntime
from control_plane.local_deploy import load_context
from analyzer.e_runtime import EConnector
from adapters.local import runtime as local
from builder.runtime import run


ROOT = Path(__file__).resolve().parents[1]
REPO = "https://github.com/Team-InfraMorph/demo-app"
SECRET = "local-runtime-smoke-signature-only"


def expect(condition, code):
    if not condition:
        raise ValueError(code)


def verify(output):
    output.mkdir(parents=True, exist_ok=False)
    adapter = LocalRuntime(root=output / "runtime", b_modules=DemoModules())
    def forbidden_duplicate_stage(*args):
        raise AssertionError("duplicate_patch_or_build_must_not_run")
    app = create_app(db_path=output / "control-plane.db", runtime=adapter,
                     patcher=forbidden_duplicate_stage, builder=forbidden_duplicate_stage)
    store = app.state.store
    client = TestClient(app)
    report = {"team_api_called": False, "aws_called": False, "publish": False,
              "mapper_planner": "explicit development fixtures", "analysis": "C runner with response replay",
              "runtime": "actual E Policy Gate/Builder/Local Docker", "checks": []}
    projects = []
    os.environ["GITHUB_WEBHOOK_SECRET"] = SECRET

    def get(did):
        return client.get(f"/api/deployments/{did}").json()

    def analysis(did):
        return client.get(f"/api/deployments/{did}/analysis").json()

    def check(name, condition):
        expect(condition, name)
        report["checks"].append(name)
        (output / "summary.json").write_text(json.dumps(report, indent=2))
        print(json.dumps({"check": name, "status": "passed"}), flush=True)

    def new_project(fault):
        adapter.fault = fault
        created = client.post("/api/projects", json={"repo_url": REPO, "targets": ["local"]}).json()
        projects.append(created["project_id"])
        did = client.post(f"/api/projects/{created['project_id']}/deploy").json()["deployment_id"]
        return created["project_id"], did

    def persistence(did):
        context = load_context(adapter.context_file(did))
        record = json.loads((Path(context.output_dir) / "fault/record.json").read_text())
        url = get(did)["targets"]["local"]["url"]
        local.smoke(url, record=record)
        state = Path(context.state_root) / ("cp-" + context.project_id)
        current = json.loads((state / "current.json").read_text())
        compose = local.compose_args(state, current["config"])
        run(compose + ["restart", "web"])
        plan = context.plan.model_copy(update={"app": state.name})
        web = next(s for s in plan.services if s.public)
        url = local.wait_for(lambda: local.local_url(run, compose, web))
        local.wait_for(lambda: local.smoke(url, record=record))
        return compose, current

    try:
        _, normal = new_project("none")
        check("normal_real_docker_live_without_retry", get(normal)["status"] == "LIVE" and analysis(normal).get("recovery") is None)
        verification = get(normal)["targets"]["local"]["verification"]
        check("d_direct_url_verification_after_real_local_deploy", verification["status"] == "ok" and verification["code"] == 200)
        before_verify = len(store.list_events(normal))
        checked = client.post(f"/api/deployments/{normal}/verify").json()["local"]["verification"]
        check("d_manual_reverify_without_duplicate_timeline", checked["status"] == "ok" and checked["code"] == 200
              and len(store.list_events(normal)) == before_verify)
        review = client.get(f"/api/deployments/{normal}/patch").json()["local"]
        check("normal_actual_e_validated_patch_visible", review["verified"] and review["applied"] and review["phase"] == "initial")
        pid, first = new_project("first")
        value = analysis(first)
        check("v1_real_docker_one_retry_live", get(first)["status"] == "LIVE" and value["recovery"]["attempts"] == 1)
        check("usage_initial_plus_retry_no_team_api", value["metrics"]["api_calls"] == 0 and
              value["metrics"]["model_calls"] == value["recovery"]["initial_metrics"]["model_calls"] + value["recovery"]["retry_metrics"]["model_calls"])
        review = client.get(f"/api/deployments/{first}/patch").json()["local"]
        check("recovered_patch_and_initial_history_visible", review["phase"] == "recovery" and review["applied"] and review["initial"]["phase"] == "initial")
        check("patch_diff_binds_verified_digest", review["diff_sha256"] == json.loads((adapter.context_file(first).parent / "retry/manifest.json").read_text())["diff_sha256"])
        persistence(first)
        check("v1_initial_note_image_survive_retry_and_restart", True)
        v1 = value["intent"]["source_revision"]
        v2 = json.loads((ROOT / "schemas/fixtures/v2/intent.json").read_text())["source_revision"]
        body = json.dumps({"ref": "refs/heads/main", "before": v1, "after": v2,
            "commits": [{"added": ["src/worker.js"], "modified": [], "removed": []}],
            "repository": {"html_url": REPO}}).encode()
        signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        # Only this project is subscribed in the verifier; the other demo project
        # uses a different branch to avoid an additional background Docker run.
        with store._conn:
            for other in projects:
                if other != pid:
                    store._conn.execute("UPDATE projects SET branch='smoke-isolated' WHERE id=?", (other,))
        pushed = client.post("/api/webhooks/github", content=body, headers={"X-GitHub-Event": "push",
            "X-GitHub-Delivery": uuid.uuid4().hex, "X-Hub-Signature-256": signature}).json()
        pending = pushed["redeploys"][0]["deployment_id"]
        check("worker_push_waits_for_human_approval", get(pending)["status"] == "AWAITING_APPROVAL")
        check("no_docker_run_before_approval", store._conn.execute("SELECT 1 FROM runtime_runs WHERE deployment_id=?", (pending,)).fetchone() is None)
        client.post(f"/api/deployments/{pending}/approve")
        check("approved_v2_real_docker_retry_live", get(pending)["status"] == "LIVE" and analysis(pending)["recovery"]["attempts"] == 1)
        compose, current = persistence(pending)
        def worker_ready():
            if '"event":"note_count"' not in run(compose + ["logs", "--no-color", "worker"]).replace(" ", ""):
                raise RuntimeError("worker_pending")
            return True
        local.wait_for(worker_ready, timeout=30)
        worker = run(compose + ["ps", "-q", "worker"])
        image = run(["docker", "inspect", worker, "--format", "{{.Image}}"])
        run(compose + ["stop", "-t", "15", "worker"])
        check("v2_worker_same_image_db_and_sigterm_zero", image == current["image_id"] and run(["docker", "inspect", worker, "--format", "{{.State.ExitCode}}"]) == "0")
        rolled = client.post(f"/api/deployments/{pending}/rollback").json()["deployment_id"]
        check("rollback_old_revision_actual_docker_cached_intent", get(rolled)["status"] == "LIVE" and get(rolled)["commit_sha"] == v1 and analysis(rolled)["recovery"]["initial_metrics"]["model_calls"] == 0)
        check("earlier_v2_analysis_history_preserved", any(w["kind"] == "worker" for w in analysis(pending)["intent"]["workloads"]))
        _, failed = new_project("always")
        check("second_local_failure_stops_after_one_retry", get(failed)["status"] == "FAILED" and analysis(failed)["recovery"]["attempts"] == 1 and analysis(failed)["recovery"]["reason"] == "second_local_failure")
        check("failed_retry_does_not_promote_intent", analysis(failed)["intent"] == analysis(failed)["initial_intent"])
        review = client.get(f"/api/deployments/{failed}/patch").json()["local"]
        check("failed_recovery_patch_not_promoted", review["phase"] == "initial" and not review["applied"] and "initial" not in review)
        before = len(store.list_events(first))
        context = adapter.context_file(first)
        process = subprocess.run([sys.executable, "-m", "control_plane.local_deploy", "--context", str(context),
            "--database", str(store.path)], capture_output=True, cwd=ROOT, env=__import__("analyzer.local_verify", fromlist=["child_environment"]).child_environment())
        check("reopened_worker_duplicate_run_blocked", process.returncode != 0 and not process.stdout and len(store.list_events(first)) == before)
        stream = client.get(f"/api/deployments/{first}/events").text
        check("sse_replay_contains_recovered_event", "retry_recovered" in stream and "event: deploy" in stream)
        report.update(status="passed", project_id=pid, v1_deployment=first, v2_deployment=pending, failed_deployment=failed)
    except Exception as error:
        report.update(status="failed", error=str(error) if __import__("re").fullmatch(r"[a-z0-9_]+", str(error)) else "runtime_verification_failed")
        raise
    finally:
        for pid in projects:
            name = "cp-" + pid
            state = output / "runtime/projects" / name
            configs = sorted(state.glob("*/compose.json"))
            if configs:
                run(local.compose_args(state, configs[-1]) + ["down", "--remove-orphans"])
            for suffix in ("-db", "-uploads"):
                volume = "inframorph-" + name + suffix
                if volume in run(["docker", "volume", "ls", "--format", "{{.Name}}"]).splitlines():
                    run(["docker", "volume", "rm", volume])
        report["owned_test_resources_cleaned"] = True
        (output / "summary.json").write_text(json.dumps(report, indent=2))
        client.close()
        os.environ.pop("GITHUB_WEBHOOK_SECRET", None)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    verify(args.output_dir.absolute())


if __name__ == "__main__":
    main()
