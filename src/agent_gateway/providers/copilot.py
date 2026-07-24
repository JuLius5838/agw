"""GitHub Copilot subscription adapter (wraps LiteLLM's Copilot authenticator).

LiteLLM's ``github_copilot`` provider stores a GitHub OAuth ``access-token`` and a
derived ``api-key.json`` in ``GITHUB_COPILOT_TOKEN_DIR``. Its
``Authenticator.get_access_token`` runs the interactive device flow when no token
is present. Copilot subscription entitlement is confirmed later by
``agw models verify`` (which exercises the api-key exchange through the proxy).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_gateway.errors import AuthError
from agent_gateway.paths import Paths, read_text
from agent_gateway.providers import Provider
from agent_gateway.providers.base import AuthState, AuthStatus, ProviderAdapter

_ACCESS_TOKEN_FILE = "access-token"


class CopilotAdapter(ProviderAdapter):
    provider = Provider.copilot
    display_name = "GitHub Copilot"

    def __init__(self, authenticator_factory: Callable[[], Any] | None = None) -> None:
        self._authenticator_factory = authenticator_factory

    def process_env(self, token_dir: Path) -> dict[str, str]:
        return {"GITHUB_COPILOT_TOKEN_DIR": str(token_dir)}

    def run_device_flow(self, token_dir: Path, *, model: str | None = None) -> None:
        factory = self._authenticator_factory or self._default_authenticator
        try:
            authenticator = factory()
            # Interactive device flow; writes the access-token file into the dir
            # set by GITHUB_COPILOT_TOKEN_DIR (established by the caller).
            authenticator.get_access_token()
        except AuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AuthError(
                f"GitHub Copilot authentication failed: {exc}",
                hint=self.remediation(),
            ) from exc

    @staticmethod
    def _default_authenticator() -> Any:
        from litellm.llms.github_copilot.authenticator import Authenticator

        return Authenticator()

    def probe_staged(self, token_dir: Path) -> None:
        token_file = token_dir / _ACCESS_TOKEN_FILE
        if not token_file.is_file() or not read_text(token_file).strip():
            raise AuthError(
                "GitHub Copilot credential probe failed: no access token was written.",
                hint=self.remediation(),
            )

    def auth_state(self, paths: Paths) -> AuthState:
        token_file = self.active_token_dir(paths) / _ACCESS_TOKEN_FILE
        if token_file.is_file():
            try:
                present = bool(read_text(token_file).strip())
            except OSError:
                present = False
            if present:
                return AuthState(AuthStatus.authenticated, "GitHub Copilot access token present")
        return AuthState(AuthStatus.missing, "no GitHub Copilot credential found")
