"""Private C/E subprocess protocol. Never load .env or echo module exceptions."""
import json
from pathlib import Path
import re
import sys

from schemas import Intent, Plan, RepoMap
from .source_policy import validate_demo_intent, validate_demo_plan


def _perform(data):
    from policy_gate.gate import validate_intent, validate_patch
    from builder.runtime import build, run
    from adapters.local.runtime import compose_args, deploy
    mapping = RepoMap.model_validate(data["repo_map"])
    snapshot = Path(data["snapshot"])
    action = data["action"]
    if action == "intent":
        intent = validate_demo_intent(Intent.model_validate(data["intent"]), snapshot, mapping)
        validate_intent(intent, snapshot, mapping.commit)
        return {"ok": True}
    if action in {"patch", "build"}:
        plan = Plan.model_validate(data["plan"])
        validate_demo_plan(plan, mapping)
        if plan.source_revision != mapping.commit:
            raise ValueError("revision_mismatch")
        if action == "patch":
            validate_patch(snapshot, data["bundle"], plan)
            return {"ok": True}
        artifact = build(snapshot, data["bundle"], plan, deployment_id=data["deployment_id"])
        return {"ok": True, "artifact": artifact.model_dump(mode="json")}
    state = Path(data["state"]).absolute()
    name = data["runtime_name"]
    if (not re.fullmatch(r"[a-z][a-z0-9-]{1,62}", name) or state.name != name or
            any(p.is_symlink() for p in (state, *state.parents))):
        raise ValueError("invalid_runtime_state")
    if action == "deploy":
        plan = Plan.model_validate(data["plan"])
        validate_demo_plan(plan.model_copy(update={"app": "demo-app"}), mapping)
        if plan.app != name or plan.source_revision != mapping.commit:
            raise ValueError("runtime_binding_mismatch")
        publish = data.get("publish", False)
        if type(publish) is not bool:
            raise ValueError("invalid_publish_option")
        result = deploy(plan, data["artifact"], state, publish=publish,
                        deployment_id=data["deployment_id"])
        # Keep paths/passwords/payloads private. Only return runtime identifiers.
        return {"ok": True, "deployment": {"url": result["public_url"] or result["url"], "image_id": result["image_id"]}}
    if action == "cleanup":
        configs = sorted(state.glob("*/compose.json"))
        if configs:
            run(compose_args(state, configs[-1]) + ["down", "--remove-orphans"], timeout=45)
        return {"ok": True}
    raise ValueError("unsupported_e_action")


def perform(data):
    from policy_gate.reporting import observe, result
    reports = []
    try:
        with observe(reports.append): reply = _perform(data)
    except Exception as error:
        if not reports or reports[-1]['decision'] == 'PASS':
            reports.append(result('source', error))
        error.policy_results = reports
        raise
    if data.get('action') == 'build':
        for report in reports: report['stage'] = 'build'
    return reply | {'policy_results': reports}


def main():
    try:
        raw = sys.stdin.buffer.read(120_001)
        if len(raw) > 120_000:
            raise ValueError("e_input_limit")
        result = perform(json.loads(raw))
    except Exception as error:
        # Accept diagnostic codes only from known E/C exception types. A generic
        # ValueError/KeyError can contain paths or source text; do not reflect it.
        types = {"PolicyError", "RuntimeFailure", "SourcePolicyError"}
        code = str(error) if type(error).__name__ in types else "e_runtime_failed"
        if not re.fullmatch(r"[a-z_]{1,80}", code):
            code = "e_runtime_failed"
        result = {"ok": False, "code": code, "policy_results": getattr(error, "policy_results", [])}
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
