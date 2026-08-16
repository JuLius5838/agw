"""Unit tests for `agw doctor` checks and `agw uninstall` safety."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_gateway.config import GatewayConfig, save_config
from agent_gateway.doctor import DoctorReport, Level, run_doctor
from agent_gateway.errors import ConfigError
from agent_gateway.paths import Paths, ensure_dir, get_paths
from agent_gateway.secret_store import ensure_proxy_key
from agent_gateway.uninstall import run_uninstall

REGISTRY_TEXT = """
default_model: gpt-5.3-codex
models:
  - name: gpt-5.3-codex
    provider: chatgpt
    upstream_model: chatgpt/gpt-5.3-codex
    mode: responses
    enabled: true
"""


def _make_claude(tmp_path: Path) -> Path:
    exe = tmp_path / "claude"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    return exe


def _setup_home(tmp_path: Path, *, with_creds: bool = False) -> Paths:
    paths = get_paths({"HOME": str(tmp_path)})
    ensure_dir(paths.config_dir)
    ensure_dir(paths.state_dir)
    ensure_proxy_key(paths)
    save_config(paths, GatewayConfig(native_claude_path=str(_make_claude(tmp_path))))
    paths.models_file.write_text(REGISTRY_TEXT)
    if with_creds:
        token_dir = paths.provider_credentials_dir("chatgpt")
        ensure_dir(token_dir)
        (token_dir / "auth.json").write_text(json.dumps({"access_token": "x", "expires_at": 9e9}))
    return paths


def _by_name(report: DoctorReport, name: str) -> Level:
    for check in report.checks:
        if check.name == name:
            return check.level
    raise AssertionError(f"no check named {name}: {[c.name for c in report.checks]}")


def test_doctor_reports_unconfigured_home(tmp_path):
    report = run_doctor(
        get_paths({"HOME": str(tmp_path)}), online=False, env={"HOME": str(tmp_path)}
    )
    assert report.failed  # config missing


def test_doctor_healthy_offline(tmp_path):
    paths = _setup_home(tmp_path, with_creds=True)
    report = run_doctor(paths, online=False, env={"HOME": str(tmp_path)})
    assert not report.failed
    assert _by_name(report, "auth: ChatGPT / Codex") is Level.ok


def test_doctor_flags_missing_auth(tmp_path):
    paths = _setup_home(tmp_path, with_creds=False)
    report = run_doctor(paths, online=False, env={"HOME": str(tmp_path)})
    assert report.failed  # provider not authenticated
    assert _by_name(report, "auth: ChatGPT / Codex") is Level.fail


def test_doctor_flags_missing_native_claude(tmp_path):
    paths = _setup_home(tmp_path, with_creds=True)
    # Break the native claude path.
    save_config(paths, GatewayConfig(native_claude_path=str(tmp_path / "gone")))
    report = run_doctor(paths, online=False, env={"HOME": str(tmp_path)})
    assert _by_name(report, "native claude") is Level.fail


def test_doctor_warns_on_availablemodels_exclusion(tmp_path):
    paths = _setup_home(tmp_path, with_creds=True)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text(json.dumps({"availableModels": ["claude-x"]}))
    report = run_doctor(paths, online=False, env={"HOME": str(tmp_path)})
    assert _by_name(report, "claude availableModels") is Level.warn


def test_doctor_warns_on_conflicting_env(tmp_path):
    paths = _setup_home(tmp_path, with_creds=True)
    report = run_doctor(
        paths,
        online=False,
        env={"HOME": str(tmp_path), "CLAUDE_CODE_USE_BEDROCK": "1"},
    )
    assert _by_name(report, "env CLAUDE_CODE_USE_BEDROCK") is Level.warn


def test_doctor_checks_copilot_credential_permissions(tmp_path):
    paths = _setup_home(tmp_path, with_creds=True)
    copilot = paths.provider_credentials_dir("copilot")
    ensure_dir(copilot)
    (copilot / "access-token").write_text("copilot-token")

    report = run_doctor(paths, online=False, env={"HOME": str(tmp_path)})

    assert _by_name(report, "copilot creds dir perms") is Level.ok


_COPILOT_REGISTRY = """
default_model: null
models:
  - name: gpt-4.1
    provider: copilot
    upstream_model: github_copilot/gpt-4.1
    mode: chat
    enabled: true
"""


def _stage_active_copilot(tmp_path: Path) -> Paths:
    paths = _setup_home(tmp_path, with_creds=False)
    paths.models_file.write_text(_COPILOT_REGISTRY)
    copilot = paths.provider_credentials_dir("copilot")
    ensure_dir(copilot)
    (copilot / "access-token").write_text("ghu_fake")
    return paths


def test_doctor_online_warns_when_copilot_subscription_inactive(tmp_path, monkeypatch):
    from agent_gateway.providers.copilot import CopilotAdapter

    paths = _stage_active_copilot(tmp_path)
    monkeypatch.setattr(
        CopilotAdapter,
        "_exchange_access_token",
        staticmethod(
            lambda token: (403, {"error_details": {"message": "Your subscription has ended."}})
        ),
    )

    report = run_doctor(paths, online=True, env={"HOME": str(tmp_path)})

    assert _by_name(report, "auth: GitHub Copilot") is Level.warn
    detail = next(c.detail for c in report.checks if c.name == "auth: GitHub Copilot")
    assert "subscription has ended" in detail


def test_doctor_offline_does_not_exchange_copilot_token(tmp_path, monkeypatch):
    from agent_gateway.providers.copilot import CopilotAdapter

    paths = _stage_active_copilot(tmp_path)

    def _must_not_call(_token: str) -> tuple[int, object]:
        raise AssertionError("offline doctor must not perform a network exchange")

    monkeypatch.setattr(CopilotAdapter, "_exchange_access_token", staticmethod(_must_not_call))

    report = run_doctor(paths, online=False, env={"HOME": str(tmp_path)})

    # Credential present is enough offline; no entitlement network call is made.
    assert _by_name(report, "auth: GitHub Copilot") is Level.ok


# --------------------------------------------------------------------------- #
# uninstall
# --------------------------------------------------------------------------- #
def test_uninstall_preserves_credentials_by_default(tmp_path):
    paths = _setup_home(tmp_path, with_creds=True)
    copilot = paths.provider_credentials_dir("copilot")
    ensure_dir(copilot)
    (copilot / "access-token").write_text("copilot-token")

    result = run_uninstall(paths, credentials=False, env={"HOME": str(tmp_path)})

    assert paths.credentials_dir.exists()  # preserved
    assert (copilot / "access-token").is_file()
    assert result.credentials_removed is False
    assert not paths.config_file.exists()  # generated runtime removed
    assert not paths.models_file.exists()


def test_uninstall_credentials_requires_acknowledgement(tmp_path):
    paths = _setup_home(tmp_path, with_creds=True)
    with pytest.raises(ConfigError):
        run_uninstall(
            paths,
            credentials=True,
            acknowledged=False,
            env={"HOME": str(tmp_path)},
        )
    assert paths.credentials_dir.exists()  # untouched on refusal


def test_uninstall_credentials_with_acknowledgement(tmp_path):
    paths = _setup_home(tmp_path, with_creds=True)
    copilot = paths.provider_credentials_dir("copilot")
    ensure_dir(copilot)
    (copilot / "access-token").write_text("copilot-token")

    result = run_uninstall(
        paths,
        credentials=True,
        acknowledged=True,
        env={"HOME": str(tmp_path)},
    )
    assert result.credentials_removed is True
    assert not paths.credentials_dir.exists()


# --------------------------------------------------------------------------- #
# External-subagent diagnostics (offline only)
# --------------------------------------------------------------------------- #
def _doctor_home(tmp_path, *, models=("gpt-5.6-sol",), rendered_config=True):
    """A configured HOME with an offline-inspectable runtime."""
    from agent_gateway.config import GatewayConfig, save_config
    from agent_gateway.paths import get_paths

    paths = get_paths({"HOME": str(tmp_path)})
    claude = tmp_path / "claude"
    claude.parent.mkdir(parents=True, exist_ok=True)
    claude.write_text("#!/bin/sh\n")
    claude.chmod(0o755)
    save_config(paths, GatewayConfig(port=4000, native_claude_path=str(claude)))
    paths.models_file.write_text(
        "default_model: null\nmodels:\n"
        + "".join(
            f"  - name: {name}\n"
            f"    provider: chatgpt\n"
            f"    upstream_model: chatgpt/{name}\n"
            f"    mode: responses\n"
            f"    enabled: true\n"
            for name in models
        )
    )
    if rendered_config:
        paths.generated_litellm_config.parent.mkdir(parents=True, exist_ok=True)
        paths.generated_litellm_config.write_text("model_list: []\n")
    return paths, claude


def _identity(claude):
    from agent_gateway.claude_compat import ClaudeExecutableIdentity

    return ClaudeExecutableIdentity(
        configured_path=str(claude),
        resolved_path=str(claude.resolve()),
        sha256="b" * 64,
        version="2.1.232 (Claude Code)",
    )


def _seed_record(paths, claude, model, *, outcome="passed", fingerprint=None):
    from agent_gateway.claude_compat import (
        COMPATIBILITY_SCHEMA,
        CompatibilityIdentity,
        CompatibilityRecord,
        utc_now,
        write_compatibility_record,
    )
    from agent_gateway.external_subagents import DEFINITION_CONTRACT_VERSION
    from agent_gateway.providers import Provider

    write_compatibility_record(
        paths,
        CompatibilityRecord(
            schema=COMPATIBILITY_SCHEMA,
            identity=CompatibilityIdentity(
                model=model,
                provider=Provider.chatgpt,
                claude=_identity(claude),
                runtime_fingerprint=fingerprint or _fingerprint(paths),
                definition_contract=DEFINITION_CONTRACT_VERSION,
            ),
            verified_at=utc_now(),
            outcome=outcome,
            detail="exact native subagent route verified" if outcome == "passed" else "nope",
        ),
    )


def _fingerprint(paths):
    from agent_gateway import proxy

    return proxy.runtime_fingerprint(paths.generated_litellm_config.read_text())


def _detail(report, name):
    return next(check.detail for check in report.checks if check.name == name)


def test_doctor_reports_each_active_model_subagent_status(tmp_path, monkeypatch):
    from agent_gateway import doctor as doctor_mod

    paths, claude = _doctor_home(tmp_path, models=("gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra"))
    monkeypatch.setattr(doctor_mod, "resolve_claude_identity", lambda _config: _identity(claude))
    _seed_record(paths, claude, "gpt-5.6-sol")
    _seed_record(paths, claude, "gpt-5.6-terra", fingerprint="d" * 64)

    report = run_doctor(paths, online=False, env={"HOME": str(tmp_path)})

    assert _detail(report, "external subagent gpt-5.6-sol") == "current"
    assert _detail(report, "external subagent gpt-5.6-luna") == "external subagent not verified"
    assert _detail(report, "external subagent gpt-5.6-terra") == (
        "external subagent evidence is stale"
    )


def test_doctor_reports_a_policy_exclusion(tmp_path, monkeypatch):
    from agent_gateway import doctor as doctor_mod

    paths, claude = _doctor_home(tmp_path)
    monkeypatch.setattr(doctor_mod, "resolve_claude_identity", lambda _config: _identity(claude))
    _seed_record(paths, claude, "gpt-5.6-sol")
    settings = tmp_path / ".claude"
    settings.mkdir(parents=True, exist_ok=True)
    (settings / "settings.json").write_text(json.dumps({"availableModels": ["sonnet"]}))

    report = run_doctor(paths, online=False, env={"HOME": str(tmp_path)})
    assert "excluded by Claude availableModels" in _detail(report, "external subagent gpt-5.6-sol")


def test_doctor_reports_an_inspectable_reserved_id_collision(tmp_path, monkeypatch):
    from agent_gateway import doctor as doctor_mod

    paths, claude = _doctor_home(tmp_path)
    monkeypatch.setattr(doctor_mod, "resolve_claude_identity", lambda _config: _identity(claude))
    _seed_record(paths, claude, "gpt-5.6-sol")
    settings = tmp_path / ".claude"
    settings.mkdir(parents=True, exist_ok=True)
    (settings / "settings.json").write_text(
        json.dumps({"agents": {"agw-gpt-5-6-sol-66127d2195a1": {"description": "mine"}}})
    )

    report = run_doctor(paths, online=False, env={"HOME": str(tmp_path)})
    collision = _detail(report, "external subagent id collision")
    assert "agw-gpt-5-6-sol-66127d2195a1" in collision
    assert "settings.json" in collision
    assert "mine" not in collision  # only path and id, never the definition


def test_doctor_degrades_when_the_runtime_fingerprint_is_unavailable(tmp_path, monkeypatch):
    from agent_gateway import doctor as doctor_mod

    paths, claude = _doctor_home(tmp_path, rendered_config=False)
    monkeypatch.setattr(doctor_mod, "resolve_claude_identity", lambda _config: _identity(claude))

    report = run_doctor(paths, online=False, env={"HOME": str(tmp_path)})
    names = [check.name for check in report.checks]
    assert "external subagent" in names
    assert _detail(report, "external subagent") == (
        "external subagent status unavailable until the proxy has been started once"
    )
    assert not any(name.startswith("external subagent gpt-") for name in names)


def test_offline_doctor_makes_no_model_or_provider_call(tmp_path, monkeypatch):
    from agent_gateway import doctor as doctor_mod

    paths, claude = _doctor_home(tmp_path)
    monkeypatch.setattr(doctor_mod, "resolve_claude_identity", lambda _config: _identity(claude))
    _seed_record(paths, claude, "gpt-5.6-sol")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("offline doctor must not verify or call a model")

    monkeypatch.setattr("agent_gateway.subagent_probe.verify_native_subagent", forbidden)
    monkeypatch.setattr("httpx.Client", forbidden)

    report = run_doctor(paths, online=False, env={"HOME": str(tmp_path)})
    assert _detail(report, "external subagent gpt-5.6-sol") == "current"


def test_doctor_env_wording_covers_definition_level_overrides(tmp_path):
    report = run_doctor(
        get_paths({"HOME": str(tmp_path)}),
        online=False,
        env={"HOME": str(tmp_path), "CLAUDE_CODE_SUBAGENT_MODEL": "haiku"},
    )
    detail = _detail(report, "env CLAUDE_CODE_SUBAGENT_MODEL")
    assert "definition-level and per-invocation subagent models" in detail


def test_uninstall_removes_generated_external_subagent_state(tmp_path):
    from agent_gateway.paths import atomic_write_text

    paths = get_paths({"HOME": str(tmp_path)})
    save_config(paths, GatewayConfig(port=4000))
    ensure_proxy_key(paths)

    record = paths.compatibility_file("gpt-5.6-sol", "chatgpt")
    ensure_dir(record.parent)
    atomic_write_text(record, "{}")
    ensure_dir(paths.probe_dir)
    atomic_write_text(paths.probe_dir / f"{'0' * 32}.ndjson", "")

    result = run_uninstall(paths, credentials=False, env={"HOME": str(tmp_path)})

    assert not paths.external_subagents_dir.exists()
    assert str(paths.external_subagents_dir) in result.removed
    # Credentials are untouched without --credentials.
    assert paths.credentials_dir.exists()
