"""ChatGPT / Codex subscription adapter (wraps LiteLLM's ChatGPT authenticator).

LiteLLM's ``chatgpt`` provider stores an ``auth.json`` (access/refresh/id tokens
and ``expires_at``) in ``CHATGPT_TOKEN_DIR``. Its ``Authenticator.get_access_token``
runs the interactive device flow when no valid token is present — which is exactly
what ``agw auth chatgpt`` drives in the foreground.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_gateway.errors import AuthError
from agent_gateway.paths import Paths, read_text
from agent_gateway.providers import Provider
from agent_gateway.providers.base import AuthState, AuthStatus, ProviderAdapter

_AUTH_FILE = "auth.json"
_EXPIRY_SKEW_SECONDS = 60


def _jwt_exp(token: str) -> float | None:
    """Best-effort decode of a JWT's ``exp`` claim; ``None`` if not derivable."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:  # noqa: BLE001 - any decode failure means "unknown expiry"
        return None
    exp = claims.get("exp")
    return float(exp) if isinstance(exp, int | float) else None


class ChatGPTAdapter(ProviderAdapter):
    provider = Provider.chatgpt
    display_name = "ChatGPT / Codex"

    def __init__(self, authenticator_factory: Callable[[], Any] | None = None) -> None:
        # Injectable for tests; defaults to LiteLLM's real authenticator.
        self._authenticator_factory = authenticator_factory

    def process_env(self, token_dir: Path) -> dict[str, str]:
        return {"CHATGPT_TOKEN_DIR": str(token_dir), "CHATGPT_AUTH_FILE": _AUTH_FILE}

    def run_device_flow(self, token_dir: Path, *, model: str | None = None) -> None:
        factory = self._authenticator_factory or self._default_authenticator
        try:
            authenticator = factory()
            # Interactive: prints the short-lived verification URL/code to the TTY,
            # polls, and writes auth.json into CHATGPT_TOKEN_DIR (set by the caller).
            authenticator.get_access_token()
        except AuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AuthError(
                f"ChatGPT authentication failed: {exc}",
                hint=self.remediation(),
            ) from exc

    @staticmethod
    def _default_authenticator() -> Any:
        from litellm.llms.chatgpt.authenticator import Authenticator

        return Authenticator()

    def probe_staged(self, token_dir: Path) -> None:
        state = self._state_from_dir(token_dir)
        if state.status is not AuthStatus.authenticated:
            raise AuthError(
                f"ChatGPT credential probe failed: {state.detail}",
                hint=self.remediation(),
            )

    def auth_state(self, paths: Paths) -> AuthState:
        return self._state_from_dir(self.active_token_dir(paths))

    def _state_from_dir(self, token_dir: Path) -> AuthState:
        auth_file = token_dir / _AUTH_FILE
        if not auth_file.is_file():
            return AuthState(AuthStatus.missing, "no ChatGPT credential found")
        try:
            data = json.loads(read_text(auth_file))
        except (OSError, json.JSONDecodeError):
            return AuthState(AuthStatus.missing, "ChatGPT credential is unreadable")
        if not isinstance(data, dict) or not data.get("access_token"):
            return AuthState(AuthStatus.missing, "ChatGPT credential has no access token")

        access_token = str(data["access_token"])
        expires_at = data.get("expires_at")
        if not isinstance(expires_at, int | float):
            expires_at = _jwt_exp(access_token)

        expired = expires_at is not None and time.time() >= float(expires_at) - _EXPIRY_SKEW_SECONDS
        if expired and not data.get("refresh_token"):
            return AuthState(AuthStatus.expired, "ChatGPT access token expired; re-login required")
        return AuthState(AuthStatus.authenticated, "ChatGPT credential present")
