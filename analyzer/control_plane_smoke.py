"""Development verification of the actual D API + C CLI + Docker recovery.

Run from a trusted checkout containing both C and D modules. B/E remain fixed
fixture stand-ins. No server, tunnel, team model API or AWS is started.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

from schemas import DeployEvent, Intent, Plan, RepoMap
from .recovery_smoke import PipelineContext
from .retry_store import RetryStore
from code_patch.local_smoke import environment

ROOT = Path(__file__).resolve().parents[1]
REPO_URL = "https://github.com/Team-InfraMorph/demo-app"
SCENARIOS = {"v1-recovered": ("v1", False), "v2-recovered": ("v2", False),
             "v2-failed": ("v2", True), "analysis-failed": ("v1", False)}


@contextmanager
def isolated_environment():
    original = dict(os.environ)
    clean = environment() | {"INFRAMORPH_MODULES_ROOT": str(ROOT), "INFRAMORPH_ANALYZER_LIVE": "0",
                             "GITHUB_WEBHOOK_SECRET": uuid.uuid4().hex}
    os.environ.clear()
    os.environ.update(clean)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


def read_sse(text):
    events, terminal = [], None
    for block in text.strip().split("\n\n"):
        fields = dict(line.split(":", 1) for line in block.splitlines() if ":" in line)
        kind = fields.get("event", "").strip()
        if kind == "deploy":
            event = DeployEvent.model_validate_json(fields["data"].strip())
            events.append({"seq": int(fields["id"]), "event": event.model_dump(mode="json", exclude_none=True)})
        elif kind == "end":
            terminal = json.loads(fields["data"])["status"]
    return events, terminal


def run_scenario(name, output, model_results=None):
    # Deferred optional dependencies: base Analyzer users do not need FastAPI.
    from fastapi.testclient import TestClient
    from control_plane.analysis import fixture_analyzer, run_analyzer
    from control_plane.app import create_app
    from control_plane.fake_push import build_payload, sign

    output.mkdir()
    case, fail_again = SCENARIOS[name]
    fixture = ROOT / "tests/fixtures/analyzer" / case
    mapping = RepoMap.model_validate_json((fixture / "repo_map.json").read_text())
    report = {"scenario": name, "case": case, "status": "error", "team_api_called": False,
              "aws_called": False, "scope": "actual D API/SQLite/SSE + C CLI; fixed fixture B/E callbacks",
              "deployer_calls": 0}
    app = None
    client = None
    try:
        def analyzer(deployment, folder):
            if name != "analysis-failed":
                return fixture_analyzer(deployment, folder)
            # Actual C process fails commit binding; no model API is called.
            invalid = mapping.model_copy(update={"commit": "0" * 40})
            path = output / "invalid-repo-map.json"
            path.write_text(invalid.model_dump_json())
            run_analyzer(ROOT, path, fixture / "snapshot", fixture / "replay.json")
            raise AssertionError("invalid_source_was_accepted")

        def deployer_cmd(deployment, target, folder):
            deployment_id = deployment["id"]
            if target != "local" or name == "analysis-failed":
                raise AssertionError("unexpected_deployer_call")
            report["deployer_calls"] += 1
            deployment = app.state.store.get_deployment(deployment_id)
            cached = app.state.store.get_analysis(deployment["project_id"], deployment["commit_sha"])
            plans = app.state.store.get_plans(deployment_id)
            context = PipelineContext(repo_map=RepoMap.model_validate(cached["repo_map"]),
                                      intent=Intent.model_validate(cached["intent"]),
                                      plan=Plan.model_validate(plans["local"]))
            context_file = output / "pipeline-context.json"
            context_file.write_text(context.model_dump_json(indent=2))
            cmd = [sys.executable, "-m", "analyzer.recovery_smoke", "--case", case,
                   "--deployment-id", deployment_id, "--pipeline-context", str(context_file),
                   "--output-dir", str(output / "docker")]
            if model_results:
                cmd += ["--model-result-dir", str(model_results)]
            if fail_again:
                cmd += ["--fail-again"]
            return cmd

        db_path = output / "control-plane.db"
        app = create_app(db_path=db_path, analyzer=analyzer, deployer_cmd=deployer_cmd,
                         patcher=lambda *args: None, builder=lambda *args: {})
        client = TestClient(app)
        response = client.post("/api/projects", json={"repo_url": REPO_URL, "branch": "main", "targets": ["local"]})
        assert response.status_code == 201
        project = response.json()
        if case == "v1":
            response = client.post(f"/api/projects/{project['project_id']}/deploy")
            assert response.status_code == 202
            deployment_id = response.json()["deployment_id"]
        else:
            payload = build_payload(REPO_URL, "main", "0" * 40, mapping.commit,
                                    {"added": ["src/worker.js"], "modified": [], "removed": []})
            body = json.dumps(payload).encode()
            headers = {"Content-Type": "application/json", "X-GitHub-Event": "push",
                       "X-GitHub-Delivery": uuid.uuid4().hex,
                       "X-Hub-Signature-256": sign(os.environ["GITHUB_WEBHOOK_SECRET"], body)}
            assert client.post("/api/webhooks/github", content=body,
                               headers=headers | {"X-Hub-Signature-256": "sha256=" + "0" * 64}).status_code == 401
            response = client.post("/api/webhooks/github", content=body, headers=headers)
            assert response.status_code == 202
            deployment_id = response.json()["redeploys"][0]["deployment_id"]
            assert response.json()["redeploys"][0]["mode"] == "full_analysis"
            duplicate = client.post("/api/webhooks/github", content=body, headers=headers)
            assert duplicate.json()["ignored"] == "duplicate"
            report["signed_push_and_duplicate_guard_passed"] = True
        report["deployment_id"] = deployment_id
        deployment = client.get(f"/api/deployments/{deployment_id}").json()
        analysis = client.get(f"/api/deployments/{deployment_id}/analysis").json()
        plans = client.get(f"/api/deployments/{deployment_id}/plans").json()
        events = app.state.store.list_events(deployment_id)
        expected_status = "FAILED" if fail_again or name == "analysis-failed" else "LIVE"
        assert deployment["status"] == expected_status
        assert deployment["targets"]["local"]["status"] == expected_status
        assert analysis["metrics"]["backend"] == "replay" and analysis["metrics"]["api_calls"] == 0
        if name == "analysis-failed":
            assert report["deployer_calls"] == 0 and all(e["event"]["step"] == "analyze" and e["event"]["status"] == "fail" for e in events) and not plans and analysis["intent"] is None
            assert analysis["metrics"]["error"]
            report["analysis_failure_prevents_deployer"] = True
        else:
            assert report["deployer_calls"] == 1 and events
            assert analysis["intent"]["source_revision"] == mapping.commit
            assert Plan.model_validate(plans["local"]).source_revision == mapping.commit
            for item in events:
                checked = DeployEvent.model_validate(item["event"])
                assert checked.deployment_id == deployment_id and checked.target.value == "local"
            # D now records analyze/policy and direct URL checks around the worker.
            recovery_events = [e for e in events if '"retry_attempt":' in (e["event"].get("detail") or "")]
            final_event = recovery_events[-1]["event"]
            final_detail = json.loads(final_event["detail"])
            assert final_detail["code"] == ("second_local_failure" if fail_again else "retry_recovered")
            assert final_event["status"] == ("fail" if fail_again else "ok")
            assert fail_again or not any(e["event"]["status"] == "fail" for e in recovery_events)
            docker_report = json.loads((output / "docker" / case / "report.json").read_text())
            assert docker_report["status"] == "passed" and not docker_report["cleanup_errors"]
            assert docker_report["pipeline_context_provided"] and docker_report["restart_does_not_retry_again"]
            state = RetryStore(output / "docker" / case / "state/retries.sqlite").inspect(deployment_id)
            assert state["attempts"] == 1
            report["recovery_metrics"] = docker_report["analysis_metrics"]
            report["retry_attempts"] = state["attempts"]
            report["docker_checks_passed"] = True
            if not fail_again:
                assert deployment["targets"]["local"]["url"] == final_event["url"]
                report["target_url_saved"] = True
        stream = client.get(f"/api/deployments/{deployment_id}/events")
        streamed, terminal = read_sse(stream.text)
        assert stream.status_code == 200 and streamed == events and terminal == expected_status
        if events:
            cursor = events[len(events) // 2]["seq"]
            resumed = client.get(f"/api/deployments/{deployment_id}/events", headers={"Last-Event-ID": str(cursor)})
            following, terminal = read_sse(resumed.text)
            assert following == [e for e in events if e["seq"] > cursor] and terminal == expected_status
        report["sse_and_resume_passed"] = True
        (output / "events.json").write_text(json.dumps(events, indent=2) + "\n")
        (output / "analysis.json").write_text(json.dumps(analysis, indent=2) + "\n")
        client.close()
        client = None
        app.state.store.close()
        app = create_app(db_path=db_path, analyzer=analyzer, deployer_cmd=deployer_cmd,
                         patcher=lambda *args: None, builder=lambda *args: {})
        client = TestClient(app)
        assert client.get(f"/api/deployments/{deployment_id}").json() == deployment
        assert client.get(f"/api/deployments/{deployment_id}/analysis").json() == analysis
        assert app.state.store.list_events(deployment_id) == events
        report.update(status="passed", control_plane_status=expected_status, event_count=len(events),
                      analysis_metrics=analysis["metrics"], database_restart_preserves_result=True)
    except Exception as error:
        # No arbitrary subprocess/model/source exception text in reports.
        report["error_type"] = type(error).__name__
    finally:
        if client:
            client.close()
        if app:
            app.state.store.close()
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=(*SCENARIOS, "all"), default="all")
    parser.add_argument("--model-result-dir", type=Path, help="Optional recorded v1/v2/intent.json")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    output = (args.output_dir or ROOT / ".local/control-plane-smoke" / uuid.uuid4().hex[:12]).resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {"results": [], "team_api_called": False, "aws_called": False,
              "scope": "local ASGI API client + actual Analyzer CLI + Docker; B/E stand-ins"}
    try:
        with isolated_environment():
            for name in (SCENARIOS if args.scenario == "all" else (args.scenario,)):
                item = run_scenario(name, output / name, args.model_result_dir.resolve() if args.model_result_dir else None)
                report["results"].append(item)
                print(json.dumps({"scenario": name, "status": item["status"], "error_type": item.get("error_type")}),
                      file=sys.stderr, flush=True)
    except ImportError:
        report["error"] = "control_plane_or_dependencies_unavailable"
    report["implementation_sha256"] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for folder in (ROOT / "analyzer", ROOT / "control_plane") for p in sorted(folder.glob("*.py"))}
    (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"output_dir": str(output), "team_api_called": False, "aws_called": False,
                      "error": report.get("error")}), flush=True)
    return 0 if report["results"] and all(r["status"] == "passed" for r in report["results"]) and not report.get("error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
