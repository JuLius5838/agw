"""Unit tests for authentication orchestration and provider adapters.

The orchestration (staging, atomic swap, preserve-on-cancel, TTY gate) is tested
with a fake adapter so no network or real OAuth is involved. The real ChatGPT
and Copilot adapters are covered only for their pure, offline logic (environment
construction and reading existing credentials); their live device flows are manual-only.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from agent_gateway.auth import authenticate, get_adapter, provider_process_env
from agent_gateway.errors import AuthError
from agent_gateway.paths import Paths, get_paths
from agent_gateway.providers import Provider
from agent_gateway.providers.base import AuthState, AuthStatus, ProviderAdapter
from agent_gateway.providers.chatgpt import ChatGPTAdapter
from agent_gateway.providers.copilot import CopilotAdapter

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX permission semantics")


class StubAuthenticator:
    def __init__(self, token_dir: Path, *, fail: bool = False) -> None:
        self.token_dir = token_dir
        self.fail = fail

    def get_access_token(self) -> str:
        if self.fail:
            raise RuntimeError("device flow failed")
        token = "ghu_faketoken"
        (self.token_dir / "access-token").write_text(token)
        return token


class FakeAdapter(ProviderAdapter):
    """A fake provider whose 'device flow' just writes a token file (or fails)."""

    provider = Provider.chatgpt
    display_name = "Fake Provider"

    def __init__(self, *, behavior: str = "success", token: str = "tok-new") -> None:
        self.behavior = behavior
        self.token = token
        self.device_flow_called = False

    def process_env(self, token_dir: Path) -> dict[str, str]:
        return {"FAKE_TOKEN_DIR": str(token_dir)}

    def run_device_flow(self, token_dir: Path, *, model: str | None = None) -> None:
        self.device_flow_called = True
        if self.behavior == "cancel":
            raise AuthError("user cancelled")
        payload = {} if self.behavior == "invalid" else {"access_token": self.token}
        (token_dir / "auth.json").write_text(json.dumps(payload))

    def probe_staged(self, token_dir: Path) -> None:
        data = json.loads((token_dir / "auth.json").read_text())
        if not data.get("access_token"):
            raise AuthError("probe failed: no token")

    def auth_state(self, paths: Paths) -> AuthState:
        auth_file = self.active_token_dir(paths) / "auth.json"
        if not auth_file.is_file():
            return AuthState(AuthStatus.missing, "none")
        data = json.loads(auth_file.read_text())
        status = AuthStatus.authenticated if data.get("access_token") else AuthStatus.missing
        return AuthState(status, "present")


def _paths(tmp_path: Path) -> Paths:
    return get_paths({"HOME": str(tmp_path)})


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def test_success_creates_active_credentials(tmp_path):
    paths = _paths(tmp_path)
    state = authenticate(paths, FakeAdapter(), isatty=True)
    assert state.status is AuthStatus.authenticated
    assert (paths.credentials_dir / "chatgpt" / "auth.json").is_file()


def test_touches_only_its_own_provider_tree(tmp_path):
    paths = _paths(tmp_path)
    copilot = paths.credentials_dir / "copilot"
    copilot.mkdir(parents=True)
    unrelated = paths.credentials_dir / "other-provider"
    unrelated.mkdir(parents=True)
    authenticate(paths, FakeAdapter(), isatty=True)
    assert (paths.credentials_dir / "chatgpt").is_dir()
    assert copilot.is_dir()
    assert unrelated.is_dir()


def test_cancel_never_reports_authenticated(tmp_path):
    paths = _paths(tmp_path)
    with pytest.raises(AuthError):
        authenticate(paths, FakeAdapter(behavior="cancel"), isatty=True)
    # No active credential, and no leftover staging directory.
    assert FakeAdapter().auth_state(paths).status is AuthStatus.missing
    assert not (paths.credentials_dir / ".chatgpt.staging").exists()


def test_forced_reauth_cancel_preserves_prior_credentials(tmp_path):
    paths = _paths(tmp_path)
    authenticate(paths, FakeAdapter(token="original-token"), isatty=True)
    active_file = paths.credentials_dir / "chatgpt" / "auth.json"
    original_bytes = active_file.read_bytes()

    with pytest.raises(AuthError):
        authenticate(paths, FakeAdapter(behavior="cancel"), force=True, isatty=True)

    assert active_file.read_bytes() == original_bytes  # byte-for-byte preserved


def test_invalid_staged_probe_fails_and_creates_no_active(tmp_path):
    paths = _paths(tmp_path)
    with pytest.raises(AuthError):
        authenticate(paths, FakeAdapter(behavior="invalid"), isatty=True)
    assert not (paths.credentials_dir / "chatgpt").exists()


def test_non_tty_fails_before_device_flow(tmp_path):
    paths = _paths(tmp_path)
    adapter = FakeAdapter()
    with pytest.raises(AuthError, match="TTY"):
        authenticate(paths, adapter, isatty=False)
    assert adapter.device_flow_called is False


def test_already_authenticated_skips_device_flow(tmp_path):
    paths = _paths(tmp_path)
    authenticate(paths, FakeAdapter(), isatty=True)
    again = FakeAdapter()
    state = authenticate(paths, again, isatty=True)  # not forced
    assert state.status is AuthStatus.authenticated
    assert again.device_flow_called is False


@posix_only
def test_credentials_have_restrictive_permissions(tmp_path):
    paths = _paths(tmp_path)
    authenticate(paths, FakeAdapter(), isatty=True)
    active = paths.credentials_dir / "chatgpt"
    assert stat.S_IMODE(active.stat().st_mode) == 0o700
    assert stat.S_IMODE((active / "auth.json").stat().st_mode) == 0o600


# --------------------------------------------------------------------------- #
# Real adapters — pure/offline logic only
# --------------------------------------------------------------------------- #
def test_chatgpt_process_env(tmp_path):
    env = ChatGPTAdapter().process_env(tmp_path)
    assert env["CHATGPT_TOKEN_DIR"] == str(tmp_path)
    assert env["CHATGPT_AUTH_FILE"] == "auth.json"


def test_chatgpt_auth_state_transitions(tmp_path):
    paths = _paths(tmp_path)
    adapter = ChatGPTAdapter()
    assert adapter.auth_state(paths).status is AuthStatus.missing

    active = adapter.active_token_dir(paths)
    active.mkdir(parents=True)
    active_file = active / "auth.json"

    active_file.write_text(json.dumps({"access_token": "x", "expires_at": 9999999999}))
    assert adapter.auth_state(paths).status is AuthStatus.authenticated

    active_file.write_text(json.dumps({"access_token": "x", "expires_at": 1}))
    assert adapter.auth_state(paths).status is AuthStatus.expired

    # Expired but refreshable -> still usable (LiteLLM refreshes at call time).
    active_file.write_text(json.dumps({"access_token": "x", "expires_at": 1, "refresh_token": "r"}))
    assert adapter.auth_state(paths).status is AuthStatus.authenticated


def test_registered_adapters_and_process_env_are_provider_isolated(tmp_path):
    paths = _paths(tmp_path)
    assert isinstance(get_adapter(Provider.chatgpt), ChatGPTAdapter)
    assert isinstance(get_adapter(Provider.copilot), CopilotAdapter)
    assert provider_process_env(paths) == {
        "CHATGPT_TOKEN_DIR": str(paths.provider_credentials_dir("chatgpt")),
        "CHATGPT_AUTH_FILE": "auth.json",
        "GITHUB_COPILOT_TOKEN_DIR": str(paths.provider_credentials_dir("copilot")),
    }


def test_copilot_process_env_and_state(tmp_path):
    paths = _paths(tmp_path)
    adapter = CopilotAdapter()
    assert adapter.process_env(tmp_path) == {"GITHUB_COPILOT_TOKEN_DIR": str(tmp_path)}
    assert adapter.auth_state(paths).status is AuthStatus.missing

    active = adapter.active_token_dir(paths)
    active.mkdir(parents=True)
    token = active / "access-token"
    token.write_text("ghu_faketoken")
    assert adapter.auth_state(paths).status is AuthStatus.authenticated

    token.write_text("  \n")
    assert adapter.auth_state(paths).status is AuthStatus.missing


def test_copilot_probe_rejects_empty_access_token(tmp_path):
    token = tmp_path / "access-token"
    token.write_text("\n")
    with pytest.raises(AuthError, match="no access token"):
        CopilotAdapter().probe_staged(tmp_path)


def test_copilot_device_flow_uses_injected_authenticator(tmp_path):
    adapter = CopilotAdapter(lambda: StubAuthenticator(tmp_path))
    adapter.run_device_flow(tmp_path)
    assert (tmp_path / "access-token").read_text() == "ghu_faketoken"


def test_copilot_device_flow_wraps_litellm_errors(tmp_path):
    adapter = CopilotAdapter(lambda: StubAuthenticator(tmp_path, fail=True))
    with pytest.raises(AuthError, match="GitHub Copilot authentication failed") as excinfo:
        adapter.run_device_flow(tmp_path)
    assert excinfo.value.hint == "agw auth copilot"


def _stage_copilot_token(paths: Paths) -> None:
    active = CopilotAdapter().active_token_dir(paths)
    active.mkdir(parents=True)
    (active / "access-token").write_text("ghu_faketoken")


def test_chatgpt_entitlement_is_a_noop_success(tmp_path):
    # Providers without a subscription gate report usable by default.
    assert ChatGPTAdapter().entitlement(_paths(tmp_path)).ok is True


def test_copilot_entitlement_active_subscription(tmp_path):
    paths = _paths(tmp_path)
    _stage_copilot_token(paths)
    ent = CopilotAdapter().entitlement(
        paths,
        exchange=lambda token: (200, {"token": "copilot-key", "endpoints": {"api": "https://x"}}),
    )
    assert ent.ok is True
    assert "active Copilot subscription" in ent.detail


def test_copilot_entitlement_reports_subscription_ended(tmp_path):
    paths = _paths(tmp_path)
    _stage_copilot_token(paths)
    body = {
        "error_details": {"message": "Your subscription has ended. You are logged in as X."},
        "message": "Resource not accessible by integration.",
    }
    ent = CopilotAdapter().entitlement(paths, exchange=lambda token: (403, body))
    assert ent.ok is False
    assert "subscription has ended" in ent.detail  # prefers the human-readable reason


def test_copilot_entitlement_missing_credential(tmp_path):
    # No access token staged: fails before any exchange is attempted.
    def _must_not_call(_token: str) -> tuple[int, object]:
        raise AssertionError("exchange must not run without a token")

    ent = CopilotAdapter().entitlement(_paths(tmp_path), exchange=_must_not_call)
    assert ent.ok is False
    assert "no GitHub Copilot credential" in ent.detail


def test_copilot_entitlement_network_error_is_inconclusive(tmp_path):
    paths = _paths(tmp_path)
    _stage_copilot_token(paths)

    def _boom(_token: str) -> tuple[int, object]:
        raise RuntimeError("dns failure")

    ent = CopilotAdapter().entitlement(paths, exchange=_boom)
    assert ent.ok is False
    assert "could not reach GitHub Copilot" in ent.detail
