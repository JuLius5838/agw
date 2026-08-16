"""Unit tests for Claude executable identity, policy, and compatibility evidence.

A generated external agent is advertised only when stored evidence still matches
the *exact* Claude build that produced it. These tests pin that identity (stable
configured path, canonical target, digest, version string), the conservative
reading of a managed ``availableModels`` allowlist, and the six status classes
that decide eligibility.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_gateway.claude_compat import (
    COMPATIBILITY_SCHEMA,
    ClaudeExecutableIdentity,
    CompatibilityIdentity,
    CompatibilityRecord,
    CompatibilityState,
    compatibility_status,
    evaluate_available_models,
    invalidate_compatibility,
    read_compatibility_record,
    resolve_claude_identity,
    utc_now,
    write_compatibility_record,
)
from agent_gateway.config import GatewayConfig, executable_sha256
from agent_gateway.errors import PrerequisiteError
from agent_gateway.external_subagents import DEFINITION_CONTRACT_VERSION
from agent_gateway.paths import Paths, get_paths
from agent_gateway.providers import Provider

VERSION = "2.1.232 (Claude Code)"
FINGERPRINT = "a" * 64


def _fake_run(stdout: str = f"{VERSION}\n", *, returncode: int = 0, stderr: str = ""):
    """Record the exact subprocess contract the caller used."""
    calls: list[dict[str, object]] = []

    def run(args, **kwargs):
        calls.append({"args": list(args), **kwargs})
        return subprocess.CompletedProcess(args=list(args), returncode=returncode, stdout=stdout,
                                           stderr=stderr)

    run.calls = calls  # type: ignore[attr-defined]
    return run


@pytest.fixture
def claude_tree(tmp_path):
    """A realistic install: a stable symlink pointing at a versioned target."""
    target = tmp_path / "versions" / "2.1.232"
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\necho claude\n")
    target.chmod(0o755)
    stable = tmp_path / "bin" / "claude"
    stable.parent.mkdir(parents=True)
    stable.symlink_to(target)
    return stable, target


@pytest.fixture
def paths(tmp_path) -> Paths:
    return get_paths({"HOME": str(tmp_path / "home")})


def _config(path: Path | str | None) -> GatewayConfig:
    return GatewayConfig(port=4000, native_claude_path=None if path is None else str(path))


def _identity(
    *,
    model: str = "gpt-5.6-sol",
    provider: Provider = Provider.chatgpt,
    claude: ClaudeExecutableIdentity | None = None,
    fingerprint: str = FINGERPRINT,
    contract: int = DEFINITION_CONTRACT_VERSION,
) -> CompatibilityIdentity:
    return CompatibilityIdentity(
        model=model,
        provider=provider,
        claude=claude
        or ClaudeExecutableIdentity(
            configured_path="/home/me/.local/bin/claude",
            resolved_path="/home/me/.local/share/claude/versions/2.1.232",
            sha256="b" * 64,
            version=VERSION,
        ),
        runtime_fingerprint=fingerprint,
        definition_contract=contract,
    )


# --------------------------------------------------------------------------- #
# Executable identity
# --------------------------------------------------------------------------- #
def test_claude_identity_keeps_the_stable_path_and_hashes_the_target(claude_tree):
    stable, target = claude_tree
    run = _fake_run()
    identity = resolve_claude_identity(_config(stable), run=run)

    assert identity.configured_path == str(stable)
    assert identity.resolved_path == str(target.resolve())
    assert identity.sha256 == executable_sha256(target)
    assert identity.version == VERSION


def test_claude_identity_uses_the_exact_bounded_subprocess_contract(claude_tree):
    stable, _target = claude_tree
    run = _fake_run()
    resolve_claude_identity(_config(stable), run=run)

    call = run.calls[0]
    assert call["args"] == [str(stable), "--version"]
    assert call["capture_output"] is True
    assert call["text"] is True
    assert call["encoding"] == "utf-8"
    assert call["errors"] == "strict"
    assert call["timeout"] == 5.0
    assert call["check"] is False


def test_claude_identity_requires_a_configured_path():
    with pytest.raises(PrerequisiteError) as excinfo:
        resolve_claude_identity(_config(None), run=_fake_run())
    assert excinfo.value.message == "the native Claude executable path is not configured."


def test_claude_identity_reports_a_dangling_symlink(tmp_path):
    stable = tmp_path / "claude"
    stable.symlink_to(tmp_path / "missing")
    with pytest.raises(PrerequisiteError) as excinfo:
        resolve_claude_identity(_config(stable), run=_fake_run())
    assert excinfo.value.message == f"native Claude executable is invalid: {stable}"
    assert excinfo.value.hint


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission semantics")
def test_claude_identity_reports_a_non_executable_target(tmp_path):
    target = tmp_path / "claude"
    target.write_text("#!/bin/sh\n")
    target.chmod(0o644)
    with pytest.raises(PrerequisiteError) as excinfo:
        resolve_claude_identity(_config(target), run=_fake_run())
    assert excinfo.value.message == f"native Claude executable is invalid: {target}"


def test_claude_identity_bounds_a_hanging_version_command(claude_tree):
    stable, _target = claude_tree

    def run(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=list(args), timeout=5.0)

    with pytest.raises(PrerequisiteError) as excinfo:
        resolve_claude_identity(_config(stable), run=run)
    assert excinfo.value.message == "could not read the native Claude version within 5s."


def test_claude_identity_reports_a_failing_version_command(claude_tree):
    stable, _target = claude_tree
    run = _fake_run(stdout="", returncode=3, stderr="boom: /secret/path")
    with pytest.raises(PrerequisiteError) as excinfo:
        resolve_claude_identity(_config(stable), run=run)
    assert excinfo.value.message == "native Claude version command failed with exit code 3."
    assert "boom" not in f"{excinfo.value.message} {excinfo.value.hint or ''}"


@pytest.mark.parametrize("stdout", ["", "   \n", "2.1.232 (Claude Code)\nextra line\n"])
def test_claude_identity_requires_one_nonempty_version_line(claude_tree, stdout):
    stable, _target = claude_tree
    with pytest.raises(PrerequisiteError) as excinfo:
        resolve_claude_identity(_config(stable), run=_fake_run(stdout))
    assert excinfo.value.message == "native Claude version output must be one non-empty line."


def test_claude_identity_rejects_undecodable_version_output(claude_tree):
    stable, _target = claude_tree

    def run(args, **kwargs):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    with pytest.raises(PrerequisiteError) as excinfo:
        resolve_claude_identity(_config(stable), run=run)
    assert excinfo.value.message == "native Claude version output must be one non-empty line."


# --------------------------------------------------------------------------- #
# Conservative availableModels policy
# --------------------------------------------------------------------------- #
def test_any_nonempty_available_models_exclusion_wins(tmp_path):
    settings = [
        (tmp_path / "user.json", {"availableModels": ["gpt-5.6-sol", "sonnet"]}),
        (tmp_path / "project.json", {"availableModels": ["sonnet"]}),
    ]
    assert evaluate_available_models("gpt-5.6-sol", settings) == (tmp_path / "project.json",)


@pytest.mark.parametrize(
    "value",
    [None, [], "sonnet", {"a": 1}, 7],
    ids=["absent", "empty-list", "string", "object", "number"],
)
def test_absent_empty_or_invalid_allowlists_do_not_exclude(tmp_path, value):
    payload: dict[str, object] = {} if value is None else {"availableModels": value}
    assert evaluate_available_models("gpt-5.6-sol", [(tmp_path / "s.json", payload)]) == ()


def test_allowlist_matching_is_exact_and_case_sensitive(tmp_path):
    settings = [(tmp_path / "s.json", {"availableModels": ["GPT-5.6-SOL", 5, None]})]
    assert evaluate_available_models("gpt-5.6-sol", settings) == (tmp_path / "s.json",)


def test_every_excluding_path_is_returned_in_input_order(tmp_path):
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    settings = [
        (first, {"availableModels": ["sonnet"]}),
        (second, {"availableModels": ["opus"]}),
    ]
    assert evaluate_available_models("gpt-5.6-sol", settings) == (first, second)


def test_no_settings_means_no_exclusion():
    assert evaluate_available_models("gpt-5.6-sol", []) == ()


# --------------------------------------------------------------------------- #
# Compatibility record schema and storage
# --------------------------------------------------------------------------- #
def _passed(identity: CompatibilityIdentity) -> CompatibilityRecord:
    return CompatibilityRecord(
        schema=COMPATIBILITY_SCHEMA,
        identity=identity,
        verified_at=datetime(2026, 8, 14, 12, 0, 0, tzinfo=UTC),
        outcome="passed",
        detail="exact native subagent route verified",
    )


def test_record_round_trips_through_the_documented_json(paths):
    identity = _identity()
    write_compatibility_record(paths, _passed(identity))

    path = paths.compatibility_file(identity.model, identity.provider.value)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {
        "schema": 1,
        "identity": {
            "model": "gpt-5.6-sol",
            "provider": "chatgpt",
            "claude": {
                "configured_path": "/home/me/.local/bin/claude",
                "resolved_path": "/home/me/.local/share/claude/versions/2.1.232",
                "sha256": "b" * 64,
                "version": VERSION,
            },
            "runtime_fingerprint": FINGERPRINT,
            "definition_contract": 1,
        },
        "verified_at": "2026-08-14T12:00:00Z",
        "outcome": "passed",
        "detail": "exact native subagent route verified",
    }
    assert read_compatibility_record(paths, "gpt-5.6-sol", Provider.chatgpt) == _passed(identity)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission semantics")
def test_record_and_its_directories_are_restrictive(paths):
    write_compatibility_record(paths, _passed(_identity()))
    path = paths.compatibility_file("gpt-5.6-sol", "chatgpt")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.external_subagents_dir.stat().st_mode) == 0o700


def test_record_holds_no_prompt_response_or_credential_field(paths):
    write_compatibility_record(paths, _passed(_identity()))
    text = paths.compatibility_file("gpt-5.6-sol", "chatgpt").read_text(encoding="utf-8")
    for forbidden in ("prompt", "response", "token", "key", "authorization", "tool"):
        assert forbidden not in text.lower()


def test_reading_a_missing_record_returns_none(paths):
    assert read_compatibility_record(paths, "gpt-5.6-sol", Provider.chatgpt) is None


# --------------------------------------------------------------------------- #
# Status classification
# --------------------------------------------------------------------------- #
def test_status_is_current_for_exact_identity_and_passed_outcome(paths):
    identity = _identity()
    write_compatibility_record(paths, _passed(identity))
    status = compatibility_status(paths, identity, settings=[])
    assert status.state is CompatibilityState.current
    assert status.detail == "exact native subagent route verified"
    assert status.model == "gpt-5.6-sol"
    assert status.provider is Provider.chatgpt


def test_status_is_missing_without_a_record(paths):
    status = compatibility_status(paths, _identity(), settings=[])
    assert status.state is CompatibilityState.missing
    assert status.detail == "external subagent not verified"
    assert status.record is None


def test_status_is_policy_excluded_before_any_storage_lookup(paths, tmp_path):
    identity = _identity()
    write_compatibility_record(paths, _passed(identity))
    settings = [(tmp_path / "user.json", {"availableModels": ["sonnet"]})]
    status = compatibility_status(paths, identity, settings=settings)
    assert status.state is CompatibilityState.policy_excluded
    assert status.detail == f"excluded by Claude availableModels in: {tmp_path / 'user.json'}"


def test_policy_exclusion_lists_every_path(paths, tmp_path):
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    settings = [
        (first, {"availableModels": ["sonnet"]}),
        (second, {"availableModels": ["opus"]}),
    ]
    status = compatibility_status(paths, _identity(), settings=settings)
    assert status.detail == f"excluded by Claude availableModels in: {first}, {second}"


@pytest.mark.parametrize(
    "payload",
    ["not json at all", '"scalar"', "[]", '{"schema": 2}', '{"schema": 1}'],
    ids=["unparsable", "scalar", "list", "wrong-schema", "incomplete"],
)
def test_status_is_invalid_for_unusable_evidence(paths, payload):
    identity = _identity()
    path = paths.compatibility_file(identity.model, identity.provider.value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    status = compatibility_status(paths, identity, settings=[])
    assert status.state is CompatibilityState.invalid
    assert status.detail == "external subagent evidence is invalid"


def test_status_is_invalid_when_the_payload_names_another_model(paths):
    identity = _identity()
    record = _passed(_identity(model="gpt-5.6-luna"))
    # A structurally valid record filed under the wrong name: the payload wins,
    # so the filename can never be used to pass one model's evidence off as
    # another's.
    path = paths.compatibility_file("gpt-5.6-sol", "chatgpt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(record.model_dump(mode="json", by_alias=True)),
        encoding="utf-8",
    )
    assert read_compatibility_record(paths, "gpt-5.6-sol", Provider.chatgpt) is not None
    assert compatibility_status(paths, identity, settings=[]).state is CompatibilityState.invalid


def test_status_is_failed_for_a_stored_failure(paths):
    identity = _identity()
    write_compatibility_record(
        paths,
        CompatibilityRecord(
            schema=COMPATIBILITY_SCHEMA,
            identity=identity,
            verified_at=utc_now(),
            outcome="failed",
            detail="native Claude subagent probe timed out",
        ),
    )
    status = compatibility_status(paths, identity, settings=[])
    assert status.state is CompatibilityState.failed
    assert status.detail == "native Claude subagent probe timed out"


@pytest.mark.parametrize(
    "mutation",
    [
        {"configured_path": "/other/bin/claude"},
        {"resolved_path": "/other/versions/2.1.233"},
        {"sha256": "c" * 64},
        {"version": "2.1.233 (Claude Code)"},
    ],
    ids=["configured-path", "resolved-path", "digest", "version"],
)
def test_every_claude_identity_component_makes_evidence_stale(paths, mutation):
    stored = _identity()
    write_compatibility_record(paths, _passed(stored))
    current = _identity(
        claude=ClaudeExecutableIdentity(
            **{
                "configured_path": stored.claude.configured_path,
                "resolved_path": stored.claude.resolved_path,
                "sha256": stored.claude.sha256,
                "version": stored.claude.version,
                **mutation,
            }
        )
    )
    status = compatibility_status(paths, current, settings=[])
    assert status.state is CompatibilityState.stale
    assert status.detail == "external subagent evidence is stale"


@pytest.mark.parametrize(
    "kwargs",
    [{"fingerprint": "d" * 64}, {"contract": 2}],
    ids=["runtime-fingerprint", "definition-contract"],
)
def test_runtime_and_contract_changes_make_evidence_stale(paths, kwargs):
    write_compatibility_record(paths, _passed(_identity()))
    status = compatibility_status(paths, _identity(**kwargs), settings=[])
    assert status.state is CompatibilityState.stale


def test_provider_change_is_a_different_record_not_a_stale_one(paths):
    write_compatibility_record(paths, _passed(_identity()))
    status = compatibility_status(paths, _identity(provider=Provider.copilot), settings=[])
    assert status.state is CompatibilityState.missing


# --------------------------------------------------------------------------- #
# Invalidation
# --------------------------------------------------------------------------- #
def test_invalidation_replaces_a_passing_record_before_reverification(paths):
    identity = _identity()
    write_compatibility_record(paths, _passed(identity))
    invalidate_compatibility(paths, identity, now=utc_now())

    status = compatibility_status(paths, identity, settings=[])
    assert status.state is CompatibilityState.failed
    assert status.detail == "verification in progress; previous success invalidated"


def test_invalidation_writes_the_current_identity_even_with_no_prior_record(paths):
    identity = _identity()
    invalidate_compatibility(paths, identity, now=utc_now())
    record = read_compatibility_record(paths, identity.model, identity.provider)
    assert record is not None
    assert record.outcome == "failed"
    assert record.identity == identity


def test_utc_now_is_timezone_aware_utc():
    now = utc_now()
    assert now.tzinfo is not None
    assert now.utcoffset() == datetime.now(UTC).utcoffset()
