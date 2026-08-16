"""Claude executable identity, managed policy, and external-subagent evidence.

Anthropic does not support routing Claude Code to non-Claude models through a
gateway, and Claude Code may silently replace a definition-level model it does
not recognize. A generated external agent is therefore advertised only from a
*successful, current* probe: evidence recorded against one exact Claude build and
one exact AGW routing fingerprint. Anything else — no evidence, unreadable
evidence, a failed probe, a changed binary, or a managed allowlist that excludes
the model — keeps the agent hidden.

Nothing here calls a model. It reads local metadata, runs a bounded
``claude --version``, and reads or writes one small JSON record per model.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from agent_gateway.config import (
    GatewayConfig,
    executable_sha256,
    validate_native_claude_path,
)
from agent_gateway.errors import ConfigError, PrerequisiteError
from agent_gateway.paths import Paths, atomic_write_text, ensure_dir
from agent_gateway.providers import Provider

COMPATIBILITY_SCHEMA: Final[int] = 1
CLAUDE_VERSION_TIMEOUT_SECONDS: Final[float] = 5.0

_HEX64: Final[str] = r"^[0-9a-f]{64}$"
_VERSION_HINT: Final[str] = "Re-run `agw setup` to reselect the native Claude executable."


def utc_now() -> datetime:
    """Aware UTC timestamp; injectable so evidence is deterministic in tests."""
    return datetime.now(tz=UTC)


class ClaudeExecutableIdentity(BaseModel):
    """Which Claude binary produced a piece of evidence.

    Both paths matter. ``configured_path`` is the stable launcher path (usually a
    symlink Claude repoints on every auto-update) and is what AGW execs;
    ``resolved_path`` plus ``sha256`` pin the actual bytes that ran. Recording
    only one of them would either miss an update or churn on every relaunch.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    configured_path: str
    resolved_path: str
    sha256: str
    version: str

    @field_validator("sha256")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        import re

        if not re.fullmatch(_HEX64, value):
            raise ValueError("sha256 must be 64 lowercase hex characters")
        return value


class CompatibilityIdentity(BaseModel):
    """The complete tuple a passing probe is bound to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str
    provider: Provider
    claude: ClaudeExecutableIdentity
    runtime_fingerprint: str
    definition_contract: int

    @field_validator("runtime_fingerprint")
    @classmethod
    def _validate_fingerprint(cls, value: str) -> str:
        import re

        if not re.fullmatch(_HEX64, value):
            raise ValueError("runtime_fingerprint must be 64 lowercase hex characters")
        return value

    @field_validator("definition_contract")
    @classmethod
    def _validate_contract(cls, value: int) -> int:
        if value < 1:
            raise ValueError("definition_contract must be >= 1")
        return value


class CompatibilityRecord(BaseModel):
    """One stored probe outcome. Never contains prompts, bodies, or credentials.

    The stored key is ``schema``, but a field of that name would shadow
    ``BaseModel.schema`` and emit a UserWarning on every import — noise on an
    ordinary ``agw`` command. The field is therefore ``schema_version`` with a
    ``schema`` alias, which keeps the on-disk JSON exactly as specified.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    schema_version: int = Field(alias="schema")
    identity: CompatibilityIdentity
    verified_at: datetime
    outcome: Literal["passed", "failed"]
    detail: str

    @field_validator("schema_version")
    @classmethod
    def _validate_schema(cls, value: int) -> int:
        if value != COMPATIBILITY_SCHEMA:
            raise ValueError(f"unsupported compatibility schema {value}")
        return value

    @field_validator("verified_at")
    @classmethod
    def _validate_verified_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("verified_at must be timezone-aware")
        return value.astimezone(UTC)


class CompatibilityState(StrEnum):
    """Why a model is or is not advertised. Only ``current`` is eligible."""

    current = "current"
    missing = "missing"
    invalid = "invalid"
    failed = "failed"
    stale = "stale"
    policy_excluded = "policy_excluded"


@dataclass(frozen=True)
class CompatibilityStatus:
    state: CompatibilityState
    model: str
    provider: Provider
    detail: str
    record: CompatibilityRecord | None


# --------------------------------------------------------------------------- #
# Executable identity
# --------------------------------------------------------------------------- #
def resolve_claude_identity(
    config: GatewayConfig,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> ClaudeExecutableIdentity:
    """Identify the configured Claude build without starting a session."""
    if not config.native_claude_path:
        raise PrerequisiteError(
            "the native Claude executable path is not configured.",
            hint="Run `agw setup`.",
        )
    try:
        configured = validate_native_claude_path(config.native_claude_path)
    except ConfigError as exc:
        raise PrerequisiteError(
            f"native Claude executable is invalid: {config.native_claude_path}",
            hint=exc.hint or _VERSION_HINT,
        ) from exc

    resolved = configured.resolve()
    try:
        completed = run(
            [str(configured), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=CLAUDE_VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PrerequisiteError(
            f"could not read the native Claude version within "
            f"{CLAUDE_VERSION_TIMEOUT_SECONDS:.0f}s.",
            hint=_VERSION_HINT,
        ) from exc
    except (UnicodeDecodeError, OSError) as exc:
        # stderr is deliberately absent from every diagnostic below: it can echo
        # local paths and environment detail that do not belong in AGW output.
        raise PrerequisiteError(
            "native Claude version output must be one non-empty line.",
            hint=_VERSION_HINT,
        ) from exc

    if completed.returncode != 0:
        raise PrerequisiteError(
            f"native Claude version command failed with exit code {completed.returncode}.",
            hint=_VERSION_HINT,
        )

    lines = [line for line in (completed.stdout or "").splitlines() if line.strip()]
    if len(lines) != 1:
        raise PrerequisiteError(
            "native Claude version output must be one non-empty line.",
            hint=_VERSION_HINT,
        )

    return ClaudeExecutableIdentity(
        configured_path=str(configured),
        resolved_path=str(resolved),
        sha256=executable_sha256(resolved),
        version=lines[0].strip(),
    )


# --------------------------------------------------------------------------- #
# Managed policy
# --------------------------------------------------------------------------- #
def evaluate_available_models(
    model: str,
    settings: Sequence[tuple[Path, dict[str, object]]],
) -> tuple[Path, ...]:
    """Return every inspectable settings file whose allowlist excludes ``model``.

    Deliberately conservative in both directions. A missing, empty, or malformed
    ``availableModels`` is not treated as an exclusion, because AGW cannot tell a
    real policy from a typo. Equally, an absent file is not treated as permission:
    managed policy that AGW cannot read is disproved only by the live probe.
    """
    excluded: list[Path] = []
    for path, payload in settings:
        allow = payload.get("availableModels")
        if not isinstance(allow, list) or not allow:
            continue
        names = {item for item in allow if isinstance(item, str)}
        if not names:
            continue
        if model not in names:
            excluded.append(path)
    return tuple(excluded)


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def write_compatibility_record(paths: Paths, record: CompatibilityRecord) -> None:
    """Persist one complete record atomically at 0600 under a 0700 tree."""
    path = paths.compatibility_file(record.identity.model, record.identity.provider.value)
    ensure_dir(paths.external_subagents_dir)
    ensure_dir(paths.compatibility_dir)
    ensure_dir(path.parent)
    payload = json.dumps(record.model_dump(mode="json", by_alias=True), indent=2, sort_keys=True)
    atomic_write_text(path, payload + "\n")


def read_compatibility_record(
    paths: Paths,
    model: str,
    provider: Provider,
) -> CompatibilityRecord | None:
    """Return a valid stored record, or ``None`` when absent or unusable."""
    path = paths.compatibility_file(model, provider.value)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        parsed: Any = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    try:
        return CompatibilityRecord.model_validate(parsed)
    except ValidationError:
        return None


def invalidate_compatibility(
    paths: Paths,
    identity: CompatibilityIdentity,
    *,
    now: datetime,
) -> None:
    """Mark a model unverified *before* re-verification starts.

    Without this, a probe that crashes or is cancelled would leave the previous
    success in place and keep advertising an agent whose evidence is no longer
    being defended.
    """
    write_compatibility_record(
        paths,
        CompatibilityRecord(
            schema=COMPATIBILITY_SCHEMA,
            identity=identity,
            verified_at=now,
            outcome="failed",
            detail="verification in progress; previous success invalidated",
        ),
    )


def compatibility_status(
    paths: Paths,
    identity: CompatibilityIdentity,
    *,
    settings: Sequence[tuple[Path, dict[str, object]]],
) -> CompatibilityStatus:
    """Classify one model's eligibility. Only ``current`` may be advertised."""

    def status(
        state: CompatibilityState,
        detail: str,
        record: CompatibilityRecord | None = None,
    ) -> CompatibilityStatus:
        return CompatibilityStatus(
            state=state,
            model=identity.model,
            provider=identity.provider,
            detail=detail,
            record=record,
        )

    # Policy first: an excluded model must never be reported as merely unverified,
    # because re-running verification would not change the outcome.
    excluded = evaluate_available_models(identity.model, settings)
    if excluded:
        joined = ", ".join(str(path) for path in excluded)
        return status(
            CompatibilityState.policy_excluded,
            f"excluded by Claude availableModels in: {joined}",
        )

    path = paths.compatibility_file(identity.model, identity.provider.value)
    if not path.exists():
        return status(CompatibilityState.missing, "external subagent not verified")

    record = read_compatibility_record(paths, identity.model, identity.provider)
    if record is None:
        return status(CompatibilityState.invalid, "external subagent evidence is invalid")

    # The filename is a hash of the model, so a payload naming a different
    # model/provider means the store was tampered with or hand-edited.
    if record.identity.model != identity.model or record.identity.provider != identity.provider:
        return status(CompatibilityState.invalid, "external subagent evidence is invalid")

    if record.identity != identity:
        return status(CompatibilityState.stale, "external subagent evidence is stale", record)

    if record.outcome == "failed":
        return status(CompatibilityState.failed, record.detail, record)

    return status(CompatibilityState.current, record.detail, record)


__all__ = [
    "CLAUDE_VERSION_TIMEOUT_SECONDS",
    "COMPATIBILITY_SCHEMA",
    "ClaudeExecutableIdentity",
    "CompatibilityIdentity",
    "CompatibilityRecord",
    "CompatibilityState",
    "CompatibilityStatus",
    "compatibility_status",
    "evaluate_available_models",
    "invalidate_compatibility",
    "read_compatibility_record",
    "resolve_claude_identity",
    "utc_now",
    "write_compatibility_record",
]
