"""Opt-in actual D/C/E Local + cloudflared check. B/model use explicit fixtures.

Only disposable demo namespaces are exposed; the control plane stays private.
Deletes only the containers and test volumes created by this invocation.
"""
import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient
from control_plane.app import create_app
from control_plane.b_bridge import DemoModules
from control_plane.runtime import LocalRuntime
from adapters.local.runtime import compose_args, smoke, rollback
from builder.runtime import run
from policy_gate.gate import require
from scripts.verify_e_boundaries import denied_paths, verify_bindings


def verify(output, *, policy_lifecycle=False):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    runtime = LocalRuntime(root=output / "runtime", b_modules=DemoModules(), publish=True)
    app = create_app(db_path=output / "cp.db", runtime=runtime)
    report = {"status": "running", "phone_verified": False, "aws_called": False,
              "b_modules": "fixtures", "model": "replay", "runtime": "actual D/C/E and public HTTPS"}
    state = None
    try:
        with TestClient(app) as client:
            project = client.post("/api/projects", json={"repo_url": "https://github.com/Team-InfraMorph/demo-app",
                                                          "targets": ["local"]}).json()
            state = output / "runtime/projects" / ("cp-" + project["project_id"])
            records = []
            for attempt in range(2):
                if attempt and policy_lifecycle:
                    previous=did
                    active=client.get('/api/policies').json()['active']
                    response=client.post(f'/api/deployments/{did}/policy-rechecks',json={'target':'local'})
                    require(response.status_code==202,'policy_recheck_request_failed')
                    job=response.json()['job_id']
                    jobs=client.get(f'/api/deployments/{did}/policy-history').json()['jobs']
                    result=next(j for j in jobs if j['id']==job)['result']
                    require(result['decision']=='PASS','policy_recheck_failed')
                    response=client.post(f'/api/deployments/{did}/policy-reviews',json={'job_id':job,'policy_digest':active['policy_digest']})
                    require(response.status_code==200,'policy_review_failed')
                    response=client.post(f'/api/deployments/{did}/policy-redeploy',json={'commit':deployment['commit_sha'],'policy_digest':active['policy_digest']})
                    require(response.status_code==202,'policy_redeploy_request_failed')
                    did=response.json()['deployment_id']
                    events=client.get(f'/api/deployments/{previous}/policy-history').json()['events']
                    require(any(e['event']=='policy_redeploy_finished' and e['payload']['policy_update_complete'] for e in events),'policy_redeploy_completion_unverified')
                    report.update(policy_recheck_passed=True,policy_review_bound=True,policy_redeploy_verified=True)
                else:
                    did = client.post(f"/api/projects/{project['project_id']}/deploy").json()["deployment_id"]
                deployment = client.get(f"/api/deployments/{did}").json()
                require(deployment["status"] == "LIVE", "public_runtime_not_live")
                target = deployment["targets"]["local"]
                url = target["url"]
                require(re.fullmatch(r"https://[a-z0-9-]+\.trycloudflare\.com", url) is not None,
                        "public_url_not_forwarded")
                require(target["verification"]["status"] == "ok", "d_public_verification_failed")
                context = json.loads(runtime.context_file(did).read_text())
                require(context["publish"] is True, "publish_context_missing")
                review = client.get(f"/api/deployments/{did}/patch").json()["local"]
                require(review["verified"] and review["applied"], "patch_not_verified_and_applied")
                events = [row["event"] for row in app.state.store.list_events(did)]
                for step in ("policy", "patch", "build", "health", "smoke"):
                    require(any(e["step"] == step and e["status"] == "ok" for e in events), "missing_stage_success")
                current = json.loads((state / "current.json").read_text())
                for record in records:
                    smoke(url, record=record)
                records.append(current["record"])
                report.update(public_url=url, denied_paths=denied_paths(url),
                              bound_services=verify_bindings(state, current["config"]))
                print(json.dumps({"attempt": attempt + 1, "public_runtime": "passed"}), flush=True)
            recovered = rollback(state)
            require(bool(recovered["public_url"]), "rollback_public_url_missing")
            for record in records:
                smoke(recovered["public_url"], record=record)
            report["public_adapter_rollback_verified"] = True
            report.update(status="passed", verified_patch_applied=True, public_redeploy_data_preserved=True,
                          d_verified_public_https=True)
    except Exception:
        report.update(status="failed", error="public_runtime_verification_failed")
        raise
    finally:
        try:
            if state is not None:
                configs = sorted(state.glob("*/compose.json"))
                if configs:
                    run(compose_args(state, configs[-1]) + ["down", "--remove-orphans"])
                project = "inframorph-" + state.name
                for suffix in ("-db", "-uploads"):
                    volume = project + suffix
                    if volume in run(["docker", "volume", "ls", "--format", "{{.Name}}"]).splitlines():
                        run(["docker", "volume", "rm", volume])
                require(not run(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + project]),
                        "test_containers_remain")
                report["owned_test_resources_cleaned"] = True
        finally:
            (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
            app.state.store.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--policy-lifecycle",action="store_true",help="Use the policy recheck/review/redeploy API for the second deployment")
    args = parser.parse_args()
    try:
        verify(args.output_dir,policy_lifecycle=args.policy_lifecycle)
    except Exception:
        print(json.dumps({"error": "public_runtime_verification_failed"}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
