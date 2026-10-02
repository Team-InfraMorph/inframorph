"""C-owned recovery coordinator; B/E supply explicit, trusted pipeline callbacks.

No implicit approvals and no default Builder/Local Adapter. Model/log output
never becomes a shell command. Existing common schemas are left unchanged.
"""
import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from schemas import BuildArtifact, DeployEvent, Intent, Plan, RepoMap
from schemas.common import ContractModel
from code_patch import patch_snapshot
from code_patch.runner import read_snapshot, tree_digest
from .backend import Backend
from .config import Limits
from .feedback import AnalysisFeedback, LocalFailure, PatchReference
from .redaction import Redactor, SECRET_NAME
from .retry_store import RetryStore
from .runner import AnalysisError, AnalysisResult, Metrics, analyze
from .snapshot import Snapshot


def fingerprint(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _contract(model, value):
    # Pydantic normally trusts existing instances, including model_copy() with
    # unchecked updates. Revalidate callback outputs through ordinary data.
    if isinstance(value, model):
        value = value.model_dump()
    return model.model_validate(value)


class Approval(ContractModel):
    approved: bool
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class BuiltPatch(ContractModel):
    artifact: BuildArtifact
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class LocalCheck(ContractModel):
    ok: bool
    failure: LocalFailure | None = None
    url: str | None = None

    @model_validator(mode="after")
    def consistent(self):
        if self.ok == (self.failure is not None):
            raise ValueError("local_check_failure_mismatch")
        if self.url:
            parsed = urlsplit(self.url)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or
                    parsed.password or parsed.query or parsed.fragment or len(self.url) > 2048):
                raise ValueError("invalid_local_check_url")
        return self


@dataclass(frozen=True)
class PatchedCandidate:
    directory: Path
    manifest: dict
    plan: Plan
    files: tuple[str, ...]

    @property
    def fingerprint(self):
        return fingerprint({"plan": self.plan.model_dump(mode="json"), "manifest": self.manifest})


@dataclass(frozen=True)
class RecoveryHooks:
    validate_intent: Callable[[Intent, str], Awaitable[Approval]]
    make_plan: Callable[[Intent], Awaitable[Plan]]
    validate_patch: Callable[[PatchedCandidate, str], Awaitable[Approval]]
    build: Callable[[PatchedCandidate, str], Awaitable[BuiltPatch]]
    check_local: Callable[[BuildArtifact, Plan], Awaitable[LocalCheck]]


@dataclass
class RecoveryResult:
    status: str
    reason: str
    retry_attempts: int
    events: list[DeployEvent]
    analysis: AnalysisResult | None = None
    plan: Plan | None = None
    patch: PatchedCandidate | None = None
    final_failure_code: str | None = None


class RecoveryStop(ValueError):
    pass


SAFE_REASONS = frozenset({
    "not_retryable", "previous_usage_unknown", "analysis_budget_exhausted",
    "source_changed_during_recovery", "unresolved_intent", "intent_policy_rejected",
    "invalid_recovery_plan", "secret_in_recovery_plan", "patch_changed_after_validation",
    "patch_policy_rejected", "build_binding_mismatch", "second_local_failure",
    "event_delivery_failed", "secret_in_local_url",
})


def _assert_patch(candidate: PatchedCandidate, mapping: RepoMap):
    try:
        root = candidate.directory / "source"
        expected = set(candidate.files)
        expected_dirs = {p.as_posix() for name in expected for p in Path(name).parents if p != Path(".")}
        # A Builder uses the whole context, including files that the source
        # filter normally ignores. Reject any added .env or other extra entry.
        for directory, dirs, names in os.walk(root, followlinks=False):
            base = Path(directory).relative_to(root)
            if any((base / d).as_posix() not in expected_dirs or (Path(directory) / d).is_symlink() for d in dirs):
                raise RecoveryStop("patch_changed_after_validation")
            if any((base / name).as_posix() not in expected for name in names):
                raise RecoveryStop("patch_changed_after_validation")
        tree = mapping.model_copy(update={"tree": list(candidate.files)})
        files, excluded = read_snapshot(root, tree)
        if excluded or tree_digest(files) != candidate.manifest["patched_digest"]:
            raise RecoveryStop("patch_changed_after_validation")
        fd = os.open(candidate.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        from .snapshot import _read_at
        try:
            diff = _read_at(fd, "patch.diff", Limits().max_snapshot_bytes)
            manifest = json.loads(_read_at(fd, "manifest.json", Limits().max_request_bytes))
        finally:
            os.close(fd)
        if (hashlib.sha256(diff).hexdigest() != candidate.manifest["diff_sha256"] or
                fingerprint(manifest) != fingerprint(candidate.manifest)):
            raise RecoveryStop("patch_changed_after_validation")
    except (OSError, ValueError, TypeError):
        raise RecoveryStop("patch_changed_after_validation") from None


def _check_plan(intent: Intent, plan: Plan, revision):
    if plan.source_revision != revision or plan.target.value != "local" or plan.app != intent.app:
        raise RecoveryStop("invalid_recovery_plan")
    left = sorted((w.name, w.kind.value, w.port, w.health, w.public, w.command) for w in intent.workloads)
    right = sorted((s.name, s.kind.value, s.port, s.health, s.public, s.command) for s in plan.services)
    db = [s for s in intent.state if s.kind == "relational_db"]
    storage = [s for s in intent.state if s.kind == "persistent_files"]
    if (left != right or set(intent.secrets) != set(plan.secrets) or bool(db) != bool(plan.db) or
            bool(storage) != bool(plan.storage) or len(db) > 1 or len(storage) > 1 or
            (storage and storage[0].path.rstrip("/") != plan.storage.path.rstrip("/"))):
        raise RecoveryStop("invalid_recovery_plan")
    if (set(plan.config) & set(plan.secrets) or any(SECRET_NAME.search(k) for k in plan.config) or
            Redactor().contains_secret(json.dumps(plan.config))):
        raise RecoveryStop("secret_in_recovery_plan")
    if "PORT" in plan.config and plan.config["PORT"] != str(next(s.port for s in plan.services if s.public)):
        raise RecoveryStop("invalid_recovery_plan")


async def recover_local(*, deployment_id: str, repo_map: RepoMap, snapshot_dir: Path,
                        previous_intent: Intent, previous_plan: Plan, previous_patch: PatchReference,
                        failure: LocalFailure, backend: Backend, hooks: RecoveryHooks,
                        store: RetryStore, output_dir: Path, limits: Limits | None = None,
                        previous_metrics: Metrics | None = None, timeout_seconds: float = 300,
                        emit: Callable[[DeployEvent], None] | None = None) -> RecoveryResult:
    """Recover ONE failed local deployment or return a terminal/not-retried result.

    Caller routes initial retryable failure here BEFORE a terminal fail event.
    It must provide immutable source, trusted failure classification and real
    B/E callbacks. The SQLite claim survives cancellation, crashes and restarts.
    """
    if not deployment_id or len(deployment_id) > 120 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in deployment_id):
        raise ValueError("invalid_deployment_id")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("invalid_recovery_timeout")
    limits = limits or Limits()
    mapping = RepoMap.model_validate(repo_map)
    source = Path(snapshot_dir).absolute()
    destination = Path(output_dir).absolute()
    if source == store.path or source in store.path.parents:
        raise ValueError("retry_store_inside_source")
    feedback = AnalysisFeedback(failure=failure, previous_intent=previous_intent,
                                previous_plan=previous_plan, previous_patch=previous_patch)
    if previous_plan.target.value != "local" or any(revision != mapping.commit for revision in (
            previous_intent.source_revision, previous_plan.source_revision, previous_patch.source_revision)):
        raise ValueError("recovery_binding_mismatch")
    snapshot = Snapshot(source, mapping.tree, limits, Redactor(backend.known_secrets))
    if previous_patch.original_digest != snapshot.digest:
        raise ValueError("recovery_snapshot_mismatch")
    # Validate/sanitize before claiming, but never persist raw failure logs.
    feedback.payload(Redactor(backend.known_secrets))
    existing = store.inspect(deployment_id)
    if existing is not None:
        if (existing["source_revision"], existing["snapshot_digest"]) != (mapping.commit, snapshot.digest):
            raise ValueError("deployment_binding_mismatch")
        return RecoveryResult("not_retried", "retry_already_used", existing["attempts"], [])
    if (destination.exists() or not destination.parent.is_dir() or destination == source or
            source in destination.parents or destination in source.parents or
            any(p.is_symlink() for p in (destination, *destination.parents))):
        raise ValueError("invalid_recovery_output")
    claimed, count = store.claim(deployment_id, mapping.commit, snapshot.digest, failure.retryable)
    result = RecoveryResult("not_retried", "retry_already_used", count, [])
    if not claimed:
        return result
    stage = failure.stage
    emitter_broken = False

    def event(step, status, code, *, phase=None, url=None):
        nonlocal emitter_broken
        detail = {"retry_attempt": count, "code": code}
        if result.final_failure_code:
            detail["failure_code"] = result.final_failure_code
        if phase:
            detail["phase"] = phase
        item = DeployEvent(deployment_id=deployment_id, ts=datetime.now(timezone.utc), target="local",
                           step=step, status=status, detail=json.dumps(detail, separators=(",", ":")), url=url)
        store.append(item)
        result.events.append(item)
        if emit and not emitter_broken:
            try:
                emit(item.model_copy(deep=True))
            except Exception:
                emitter_broken = True
                raise RecoveryStop("event_delivery_failed") from None

    async def step(name, phase, operation):
        nonlocal stage
        stage = name
        event(name, "started", "retry_started", phase=phase)
        value = await operation()
        return value

    try:
        if not failure.retryable:
            raise RecoveryStop("not_retryable")
        # D currently treats ANY fail event as final FAILED. Preserve first
        # failure as retry-in-progress metadata, then emit fail only if terminal.
        event(stage, "started", failure.code, phase="recoverable_failure")
        async with asyncio.timeout(timeout_seconds):
            budget = limits
            if backend.name == "openai":
                if previous_metrics is None or not previous_metrics.usage_complete:
                    raise RecoveryStop("previous_usage_unknown")
                spent = previous_metrics.estimated_usd
                if not math.isfinite(spent) or spent < 0 or spent >= limits.max_estimated_usd:
                    raise RecoveryStop("analysis_budget_exhausted")
                budget = replace(limits, max_estimated_usd=limits.max_estimated_usd - spent)
            result.analysis = await step("analyze", "intent", lambda: analyze(
                mapping, source, backend, budget, feedback=feedback))
            if result.analysis.metrics.snapshot_digest != snapshot.digest:
                raise RecoveryStop("source_changed_during_recovery")
            event("analyze", "ok", "intent_reanalyzed")
            intent = result.analysis.intent.model_copy(deep=True)
            intent_signature = fingerprint(intent)

            async def validate_intent():
                if intent.unknowns:
                    raise RecoveryStop("unresolved_intent")
                approved = _contract(Approval, await hooks.validate_intent(intent.model_copy(deep=True), intent_signature))
                if not approved.approved or approved.fingerprint != intent_signature:
                    raise RecoveryStop("intent_policy_rejected")
            await step("policy", "intent", validate_intent)
            event("policy", "ok", "intent_policy_approved", phase="intent")
            plan = _contract(Plan, await step("plan", "local", lambda: hooks.make_plan(intent.model_copy(deep=True)))).model_copy(deep=True)
            _check_plan(intent, plan, mapping.commit)
            result.plan = plan.model_copy(deep=True)
            event("plan", "ok", "local_plan_created")
            stage = "patch"
            event("patch", "started", "retry_started")
            manifest = patch_snapshot(source, mapping, plan, destination)
            if manifest["original_digest"] != snapshot.digest:
                raise RecoveryStop("source_changed_during_recovery")
            files = tuple(sorted(set(snapshot.files) | {c["path"] for c in manifest["changes"]}))
            result.patch = candidate = PatchedCandidate(destination, manifest, plan.model_copy(deep=True), files)
            _assert_patch(candidate, mapping)
            signature = candidate.fingerprint
            event("patch", "ok", "fresh_patch_created")

            async def validate_patch():
                approved = _contract(Approval, await hooks.validate_patch(candidate, signature))
                _assert_patch(candidate, mapping)
                if (not approved.approved or approved.fingerprint != signature or candidate.fingerprint != signature):
                    raise RecoveryStop("patch_policy_rejected")
            await step("policy", "patch", validate_patch)
            event("policy", "ok", "patch_policy_approved", phase="patch")
            built = _contract(BuiltPatch, await step("build", "patch", lambda: hooks.build(candidate, signature)))
            _assert_patch(candidate, mapping)
            if (built.fingerprint != signature or candidate.fingerprint != signature or
                    built.artifact.source_revision != mapping.commit or built.artifact.target.value != "local"):
                raise RecoveryStop("build_binding_mismatch")
            event("build", "ok", "validated_patch_built")
            checked = _contract(LocalCheck, await step("start", "local", lambda: hooks.check_local(
                built.artifact.model_copy(deep=True), plan.model_copy(deep=True))))
            if not checked.ok:
                stage = checked.failure.stage
                result.final_failure_code = checked.failure.code
                raise RecoveryStop("second_local_failure")
            if checked.url and Redactor(backend.known_secrets).contains_secret(checked.url):
                raise RecoveryStop("secret_in_local_url")
            event("start", "ok", "local_started")
            event("smoke", "ok", "retry_recovered", url=checked.url)
            result.status, result.reason = "recovered", "retry_recovered"
    except asyncio.CancelledError:
        store.finish(deployment_id, "failed", "recovery_interrupted")
        event(stage, "fail", "recovery_interrupted")
        raise
    except Exception as error:
        # Callback exceptions are not reflected; they may contain credentials.
        if isinstance(error, RecoveryStop) and str(error) in SAFE_REASONS:
            code = str(error)
        elif isinstance(error, TimeoutError):
            code = "recovery_timeout"
        elif isinstance(error, AnalysisError):
            code = "reanalysis_failed"
        else:
            code = "recovery_dependency_failed"
        result.status, result.reason = "failed", code
        event(stage, "fail", code)
    store.finish(deployment_id, result.status, result.reason)
    return result
