"""Opt-in real v1 public exposure and first-deploy failure cleanup verification.

Only resources under the supplied dedicated app/project name are inspected.
No arbitrary network scanning. No application code or secrets are printed.
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from adapters.local.runtime import compose_args, deploy, smoke, wait_for
from builder.runtime import RuntimeFailure, run
from policy_gate.gate import require


def verify_bindings(state, config):
    args = compose_args(state, config)
    ids = run(args + ["ps", "-q"]).splitlines()
    require(bool(ids), "containers_missing")
    containers = json.loads(run(["docker", "inspect", *ids]))
    checked = []
    for container in containers:
        service = container["Config"]["Labels"]["com.docker.compose.service"]
        ports = container["HostConfig"].get("PortBindings") or {}
        bindings = [b for values in ports.values() for b in (values or [])]
        require(service == "web" or not bindings, "private_service_published")
        if service == "web":
            require(
                bool(bindings) and all(b["HostIp"] == "127.0.0.1" for b in bindings),
                "web_not_loopback",
            )
        require(not container["HostConfig"].get("Privileged"), "privileged_container")
        checked.append(service)
    return sorted(checked)


def denied_paths(url):
    checked = []
    for path in ("/.env", "/.git/config", "/package.json", "/prisma/schema.prisma"):
        try:
            with urllib.request.urlopen(url + path, timeout=15) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        require(status in (403, 404), "private_http_path_exposed")
        checked.append({"path": path, "status": status})
    return checked


def verify(plan, artifact, state, output):
    require(not (state / "current.json").exists(), "dedicated_fresh_state_required")
    output.mkdir(parents=True, exist_ok=False)
    result = {"phone_verified": False}
    public_url = None

    def sink(event):
        print(event.jsonl(), end="", flush=True)
        with (output / "events.jsonl").open("a") as stream:
            stream.write(event.jsonl())

    def fail_after_public_check(args, **kwargs):
        nonlocal public_url
        value = run(args, **kwargs)
        if "up" in args:
            compose = args[: args.index("up")]

            def tunnel_url():
                urls = re.findall(
                    r"https://[a-z0-9-]+\.trycloudflare\.com",
                    run(compose + ["logs", "--no-color", "tunnel"]),
                )
                require(bool(urls), "tunnel_pending")
                return urls[-1]

            public_url = wait_for(tunnel_url)
            wait_for(lambda: smoke(public_url))
            result["v1_public_note_image"] = True
            result["denied_paths"] = denied_paths(public_url)
            config = args[args.index("-f") + 1]
            result["binding_services_checked"] = verify_bindings(state, config)
            raise RuntimeFailure("injected_first_public_failure")
        return value

    try:
        deploy(
            plan,
            artifact,
            state,
            publish=True,
            deployment_id="e-boundary-v1",
            execute=fail_after_public_check,
            sink=sink,
        )
    except RuntimeFailure as exc:
        require(
            str(exc) == "injected_first_public_failure", "unexpected_boundary_failure"
        )
    else:
        raise RuntimeFailure("expected_failure_missing")
    remaining = run(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            "label=com.docker.compose.project=inframorph-" + state.name,
        ]
    )
    require(not remaining, "failed_deployment_containers_remain")
    require(
        not (state / "current.json").exists(), "failed_deployment_marked_successful"
    )
    require(public_url is not None, "public_url_unverified")

    def stopped():
        try:
            with urllib.request.urlopen(public_url + "/health", timeout=10) as response:
                healthy = response.status == 200
        except (OSError, urllib.error.URLError):
            healthy = False
        require(not healthy, "public_app_still_reachable")

    wait_for(stopped, timeout=60)
    for suffix in ("-db", "-uploads"):
        run(["docker", "volume", "inspect", "inframorph-" + state.name + suffix])
    result.update(
        first_failure_cleaned=True,
        public_health_unreachable_after_cleanup=True,
        named_volumes_preserved=True,
    )
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    state = args.state_dir.absolute()
    plan = json.loads(args.plan.read_text())
    plan["app"] = state.name
    verify(plan, json.loads(args.artifact.read_text()), state, args.output)


if __name__ == "__main__":
    main()
