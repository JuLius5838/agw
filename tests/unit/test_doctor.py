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
