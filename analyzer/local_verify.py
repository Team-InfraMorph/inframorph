"""Development-only Codex evaluation and offline comparison of saved API results.

This sends the complete sanitized fixture in one prompt. It does NOT exercise
the production Analyzer's Read/Grep/Glob loop or prove its security properties.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import re
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import uuid

from pydantic import ValidationError
from schemas import Intent, RepoMap
from schemas.common import parse_evidence

from .config import MODEL, Limits
from .redaction import Redactor
from .runner import INSTRUCTIONS, _validate
from .snapshot import Snapshot
from .feedback import AnalysisFeedback, FEEDBACK_INSTRUCTIONS
from .trust import DATA_INSTRUCTIONS, data_message, navigation_map


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/analyzer"
MAX_OUTPUT_BYTES = 2_000_000
LOCAL_MODEL = "gpt-6-astra"
DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "shell_snapshot", "apps", "plugins", "hooks",
    "browser_use", "computer_use", "multi_agent", "image_generation", "view_image",
    "memories", "skill_search", "code_mode", "code_mode_host",
)


class VerificationError(ValueError):
    """A safe error code, without raw model output or credentials."""

    def __init__(self, code: str, details=None):
        super().__init__(code)
        self.details = details


def read_text(path: Path, limit=MAX_OUTPUT_BYTES) -> str:
    with path.open("rb") as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise VerificationError("file_size_limit")
    return data.decode("utf-8")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def prepare(case: str, *, feedback: AnalysisFeedback | None = None):
    fixture = FIXTURES / case
    mapping = RepoMap.model_validate_json(read_text(fixture / "repo_map.json"))
    snapshot = Snapshot(fixture / "snapshot", mapping.tree, Limits(), Redactor())
    return prepare_source(mapping, snapshot, feedback=feedback)


def prepare_source(mapping, snapshot, *, feedback=None):
    """Build a full-source evaluation prompt, including adversarial snapshots."""
    map_data = navigation_map(mapping, snapshot)
    # No expected Intent, replay transcript, repository instructions or .env.
    payload = {
        "repo_map": map_data,
        "unavailable_file_count": len(snapshot.excluded),
        "source_files": [
            {"path": path, "lines": [{"line": n, "text": text}
                                      for n, text in enumerate(lines, 1)]}
            for path, lines in sorted(snapshot.files.items())
        ],
    }
    instructions = (INSTRUCTIONS + DATA_INSTRUCTIONS).replace(
        "Only use Read, Grep (literal search), and Glob.",
        "All available source lines are supplied in source_files. Do not use tools.",
    ).replace("Explore actual source before answering.", "Inspect the supplied source before answering.")
    instructions = instructions.replace("source lines actually returned by Read/Grep", "supplied source lines")
    if feedback is not None:
        feedback = AnalysisFeedback.model_validate(feedback)
        if (feedback.previous_plan.target.value != "local" or
                feedback.previous_patch.original_digest != snapshot.digest or any(revision != mapping.commit for revision in (
                    feedback.previous_patch.source_revision, feedback.previous_plan.source_revision,
                    feedback.previous_intent.source_revision))):
            raise VerificationError("feedback_binding_mismatch")
        instructions += "\n" + FEEDBACK_INSTRUCTIONS.replace(
            "Reinspect source with Read/Grep/Glob.", "Reinspect the supplied source lines without tools.")
        payload["failure_feedback"] = feedback.payload(snapshot.redactor)
    instructions += "\nThis is a development evaluation. Return the Intent directly, without a wrapper.\n"
    prompt = (instructions + "\nIntent JSON schema:\n" + json.dumps(Intent.model_json_schema())
              + "\nUNTRUSTED INPUT DATA:\n"
              + data_message("repository", payload, snapshot.redactor))
    if len(prompt.encode()) > Limits().max_request_bytes:
        raise VerificationError("prompt_size_limit")
    # In this evaluation every supplied line is available to the model. This is
    # deliberately different from the production loop's tool observation log.
    snapshot.observed.update((path, n) for path, lines in snapshot.files.items()
                             for n in range(1, len(lines) + 1))
    return mapping, snapshot, prompt


def child_environment() -> dict[str, str]:
    # Preserve sign-in location, never copy auth tokens or load .env. Do not
    # inherit API keys, alternate API endpoints, app RPC settings or proxies.
    allowed = {"HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "CODEX_HOME",
               "SSL_CERT_FILE", "SSL_CERT_DIR", "CODEX_CA_CERTIFICATE"}
    return {key: value for key, value in os.environ.items() if key in allowed}


def command(binary: str, model: str, output: Path) -> list[str]:
    args = [binary, "exec", "--ignore-user-config", "--skip-git-repo-check",
            "--ephemeral", "--sandbox", "read-only", "--color", "never", "--json",
            "--model", model, "--output-last-message", str(output),
            "-c", 'forced_login_method="chatgpt"', "-c", 'model_provider="openai"',
            "-c", 'model_reasoning_effort="low"', "-c", 'web_search="disabled"',
            "-c", "project_doc_max_bytes=0", "-c", 'approval_policy="never"',
            "-c", 'history.persistence="none"', "-c", "tools.view_image=false"]
    for feature in DISABLED_FEATURES:
        args.extend(["--disable", feature])
    return args + ["-"]


def failure_code(log: str) -> str:
    text = log.lower()
    if ("not supported" in text and "model" in text) or "model_not_found" in text:
        return "codex_model_unavailable"
    if "usage limit" in text or "rate limit" in text or "quota" in text:
        return "codex_usage_limit"
    if "unauthorized" in text or "not logged in" in text or "refresh token" in text:
        return "codex_authentication_failed"
    if "error sending request" in text or "failed to connect" in text:
        return "codex_network_error"
    return "codex_execution_failed"


def run_codex(prompt: str, model: str, timeout: float):
    binary = shutil.which("codex")
    if not binary:
        raise VerificationError("codex_not_installed")
    env = child_environment()
    # A fresh directory outside the checkout prevents project config and answer
    # fixtures being discovered. Source is supplied through stdin, not shell.
    with tempfile.TemporaryDirectory(prefix="inframorph-codex-") as directory:
        work = Path(directory)
        auth = subprocess.run([binary, "login", "status"], cwd=work, env=env,
                              capture_output=True, text=True, timeout=15)
        if auth.returncode or "Logged in using ChatGPT" not in auth.stdout + auth.stderr:
            raise VerificationError("chatgpt_login_required")
        version = subprocess.run([binary, "--version"], cwd=work, env=env,
                                 capture_output=True, text=True, timeout=15)
        output = work / "intent.json"
        with tempfile.TemporaryFile() as log:
            process = subprocess.Popen(command(binary, model, output), cwd=work, env=env,
                                       stdin=subprocess.PIPE, stdout=log, stderr=log,
                                       start_new_session=True)
            try:
                process.communicate(prompt.encode(), timeout=timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise VerificationError("codex_timeout_or_interrupted") from None
            log.seek(0)
            data = log.read(MAX_OUTPUT_BYTES + 1)
        if len(data) > MAX_OUTPUT_BYTES:
            raise VerificationError("codex_log_size_limit")
        logs = data.decode("utf-8", errors="replace")
        if process.returncode:
            raise VerificationError(failure_code(logs))
        usage = None
        completed = False
        for line in logs.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") in {"error", "turn.failed"}:
                raise VerificationError(failure_code(line))
            item = event.get("item", {})
            if item.get("type") in {"command_execution", "mcp_tool_call", "web_search", "file_change"}:
                raise VerificationError("unexpected_codex_tool_use")
            if event.get("type") == "turn.completed":
                completed = True
                values = event.get("usage", {})
                usage = {key: value for key, value in values.items()
                         if key in {"input_tokens", "cached_input_tokens", "output_tokens"}
                         and type(value) is int and value >= 0}
        if not completed or not output.is_file():
            raise VerificationError("codex_incomplete")
        return read_text(output), {"cli_version": version.stdout.strip(),
                                   "authentication": "chatgpt", "usage": usage}


def requirements(intent: Intent) -> dict:
    """Compare operational fields; wording, order and valid citations may vary."""
    data = intent.model_dump(mode="json")
    workloads = [{k: v for k, v in item.items() if k != "evidence"} for item in data["workloads"]]
    state = [{k: v for k, v in item.items() if k not in {"reason", "evidence"}} for item in data["state"]]
    for item in state:
        if item["path"]:
            item["path"] = item["path"].rstrip("/")
    return {"schema_version": data["schema_version"], "source_revision": data["source_revision"],
            "app": data["app"], "runtime": data["runtime"],
            "workloads": sorted(workloads, key=lambda x: x["name"]),
            "state": sorted(state, key=lambda x: json.dumps(x, sort_keys=True)),
            "secrets": sorted(set(data["secrets"]))}


def compare(left: Intent, right: Intent) -> dict:
    a, b = requirements(left), requirements(right)
    different = [key for key in a if a[key] != b[key]]
    return {"requirements_match": not different, "different_fields": different,
            "differences": {key: {"actual": a[key], "reference": b[key]} for key in different},
            "config_match": left.config == right.config,
            "config": {"actual": left.config, "reference": right.config},
            "unknowns": {"actual": left.unknowns, "reference": right.unknowns}}


def config_review(intent: Intent, expected: Intent, snapshot: Snapshot) -> dict:
    """Fixture-only exception: prove the optional PORT from a literal source line.

    Retain the raw config difference. Never remove unknowns or change an Intent.
    This does not replace semantic source review for arbitrary repositories.
    """
    different = {key for key in intent.config.keys() | expected.config.keys()
                 if intent.config.get(key) != expected.config.get(key)}
    accepted = []
    if different == {"PORT"} and "PORT" not in expected.config:
        value = intent.config.get("PORT", "")
        ports = [w.port for w in intent.workloads if w.public]
        pattern = re.compile(r'^const port = Number\(process\.env\.PORT \|\| ([0-9]+)\);$')
        defaults = [m.group(1) for line in snapshot.files.get("src/server.js", [])
                    if (m := pattern.fullmatch(line))]
        if defaults == [value] and value.isdigit() and ports == [int(value)]:
            accepted = ["PORT"]
    return {"accepted_source_defaults": accepted,
            "unreviewed_keys": sorted(different - set(accepted))}


def evaluate(text: str, mapping, snapshot):
    try:
        intent = _validate(text, mapping, snapshot)
    except ValidationError as error:
        details = [{"type": e["type"],
                    "field": e["loc"][0] if e["loc"] and e["loc"][0] in Intent.model_fields else None}
                   for e in error.errors(include_input=False, include_context=False)]
        raise VerificationError("invalid_intent", details) from None
    except ValueError as error:
        # _validate emits fixed internal codes, never user input.
        raise VerificationError("invalid_intent", {"validation": str(error)}) from None
    excerpts = []
    for item in [*intent.workloads, *intent.state]:
        for citation in item.evidence:
            path, line = parse_evidence(citation)
            excerpts.append({"evidence": citation, "text": snapshot.files[path][line - 1]})
    return intent, excerpts


def save(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def verify_case(case: str, args, directory: Path) -> dict:
    directory.mkdir()
    started = time.monotonic()
    report = {"case": case, "backend": "codex-cli" if args.action == "run" else "saved-intent",
              "scope": "full-source fixture evaluation; not production Analyzer integration",
              "status": "error", "team_api_called_by_verifier": False,
              "api_billing_usd": None, "semantic_evidence_review": "required"}
    try:
        feedback_path = getattr(args, "feedback", None)
        feedback = AnalysisFeedback.model_validate_json(read_text(feedback_path, Limits().max_request_bytes)) if feedback_path else None
        mapping, snapshot, prompt = prepare(case, feedback=feedback)
        report.update(source_revision=mapping.commit, snapshot_digest=snapshot.digest,
                      prompt_sha256=digest(prompt), prompt_bytes=len(prompt.encode()),
                      provided_files=sorted(snapshot.files))
        if feedback:
            report["failure_feedback_provided"] = True
        if args.action == "run":
            report.update(model_requested=args.model, reasoning_effort="low",
                          production_api_model=MODEL, same_model_as_api=args.model == MODEL)
            text, metadata = run_codex(prompt, args.model, args.timeout)
            report.update(metadata)
        else:
            text = read_text(args.intent)
        intent, excerpts = evaluate(text, mapping, snapshot)
        # The reference is loaded only after the model has finished.
        expected_text = read_text(ROOT / f"schemas/fixtures/{case}/intent.json")
        expected = Intent.model_validate_json(expected_text)
        report.update(expected_sha256=digest(expected_text), schema_and_evidence_valid=True,
                      comparison=compare(intent, expected), evidence_for_review=excerpts)
        report["config_review"] = config_review(intent, expected, snapshot)
        if getattr(args, "compare", None):
            other, _ = evaluate(read_text(args.compare), mapping, snapshot)
            report["comparison_to_saved_intent"] = compare(intent, other)
        report["status"] = ("matched" if report["comparison"]["requirements_match"] else "mismatch")
        if report["status"] == "matched" and (intent.unknowns or report["config_review"]["unreviewed_keys"]):
            report["status"] = "needs_review"
        if "comparison_to_saved_intent" in report:
            compared = report["comparison_to_saved_intent"]
            if not compared["requirements_match"]:
                report["status"] = "mismatch"
            elif not compared["config_match"] and report["status"] == "matched":
                report["status"] = "needs_review"
        save(directory / "intent.json", intent.model_dump(mode="json"))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        report["error"] = str(error) if isinstance(error, VerificationError) else "invalid_verification_input"
        if isinstance(error, VerificationError) and error.details is not None:
            report["error_details"] = error.details
    report["duration_ms"] = round((time.monotonic() - started) * 1000)
    save(directory / "report.json", report)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    run = commands.add_parser("run", help="Use ChatGPT-authenticated Codex; never load an API key")
    run.add_argument("--case", choices=("v1", "v2", "all"), default="all")
    run.add_argument("--model", default=LOCAL_MODEL,
                     help="Codex model; recorded separately from the production API model")
    run.add_argument("--timeout", type=float, default=180)
    check = commands.add_parser("check", help="Offline validation of a saved Intent, including API output")
    check.add_argument("--case", choices=("v1", "v2"), required=True)
    check.add_argument("--intent", type=Path, required=True)
    check.add_argument("--compare", type=Path, help="Optional saved Codex Intent for field comparison")
    for sub in (run, check):
        sub.add_argument("--output-dir", type=Path, help="New directory; existing results are never overwritten")
        sub.add_argument("--feedback", type=Path, help="Optional bounded AnalysisFeedback JSON for one fixture case")
    args = parser.parse_args(argv)
    if args.action == "run" and (not math.isfinite(args.timeout) or not 0 < args.timeout <= 600):
        parser.error("--timeout must be finite and between 0 and 600 seconds")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = args.output_dir or ROOT / ".local/analyzer-verification" / f"{args.action}-{stamp}-{uuid.uuid4().hex[:6]}"
    try:
        destination.mkdir(parents=True, exist_ok=False)
    except OSError:
        print(json.dumps({"error": "output_directory_unavailable"}))
        return 1
    cases = ("v1", "v2") if args.case == "all" else (args.case,)
    reports = []
    for case in cases:
        report = verify_case(case, args, destination / case)
        reports.append({"case": case, "status": report["status"], "error": report.get("error")})
        print(json.dumps(reports[-1]), flush=True)
    summary = {"results": reports, "output_dir": str(destination.resolve()),
               "team_api_called_by_verifier": False,
               "note": "matched means automatic contract checks passed; semantic evidence still needs review"}
    save(destination / "summary.json", summary)
    print(json.dumps(summary), flush=True)
    return 0 if all(r["status"] == "matched" for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
