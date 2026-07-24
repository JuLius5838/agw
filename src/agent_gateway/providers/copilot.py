"""GitHub Copilot subscription adapter over LiteLLM's device flow.

LiteLLM's ``github_copilot`` provider stores a GitHub OAuth ``access-token`` and a
short-lived derived ``api-key.json`` in ``GITHUB_COPILOT_TOKEN_DIR``. Device login
writes only the GitHub token; the token→API-key exchange (which requires an active
Copilot subscription) happens later, at request time.

Because a GitHub sign-in can succeed for an account with no active Copilot plan,
this adapter adds an :meth:`entitlement` check: it performs the same exchange
``agw auth copilot`` and an online ``agw doctor`` use to report a lapsed
subscription clearly, instead of surfacing LiteLLM's opaque "no healthy
deployments" error only after a model is enabled and called.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from agent_gateway.errors import AuthError
from agent_gateway.paths import Paths, read_text
from agent_gateway.providers import Provider
from agent_gateway.providers.base import AuthState, AuthStatus, Entitlement, ProviderAdapter

_ACCESS_TOKEN_FILE = "access-token"
_ENTITLEMENT_TIMEOUT = 15.0

# Static GitHub API headers LiteLLM 1.93.0 sends for the token exchange; the
# per-request Authorization is added from the stored access token.
_COPILOT_GITHUB_HEADERS = {
    "accept": "application/json",
    "editor-version": "vscode/1.85.1",
    "editor-plugin-version": "copilot/1.155.0",
    "user-agent": "GithubCopilot/1.155.0",
    "accept-encoding": "gzip,deflate,br",
    "content-type": "application/json",
}


def _copilot_api_key_url() -> str:
    """The Copilot entitlement endpoint, read from pinned LiteLLM to avoid drift."""
    try:
        from litellm.llms.github_copilot.authenticator import DEFAULT_GITHUB_API_KEY_URL

        return str(DEFAULT_GITHUB_API_KEY_URL)
    except Exception:  # noqa: BLE001 - fall back to the documented endpoint
        return "https://api.github.com/copilot_internal/v2/token"


def _entitlement_detail(status: int, body: Any) -> str:
    """Extract GitHub's human-readable reason (e.g. "subscription has ended")."""
    if isinstance(body, dict):
        details = body.get("error_details")
        if isinstance(details, dict):
            message = details.get("message")
            if isinstance(message, str) and message:
                return message
        top = body.get("message")
        if isinstance(top, str) and top:
            return top
    return f"Copilot entitlement check failed (HTTP {status})"


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
            # LiteLLM prints only the short-lived verification URL/code and writes
            # access-token under GITHUB_COPILOT_TOKEN_DIR, scoped by the caller.
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
        if not token_file.is_file():
            self._raise_missing_probe()
        try:
            present = bool(read_text(token_file).strip())
        except OSError:
            present = False
        if not present:
            self._raise_missing_probe()

    def _raise_missing_probe(self) -> None:
        raise AuthError(
            "GitHub Copilot credential probe failed: no access token was written.",
            hint=self.remediation(),
        )

    def auth_state(self, paths: Paths) -> AuthState:
        if self._read_access_token(self.active_token_dir(paths)):
            return AuthState(AuthStatus.authenticated, "GitHub Copilot access token present")
        return AuthState(AuthStatus.missing, "no GitHub Copilot credential found")

    def entitlement(
        self,
        paths: Paths,
        *,
        exchange: Callable[[str], tuple[int, Any]] | None = None,
    ) -> Entitlement:
        """Exchange the stored GitHub token for a Copilot API key to confirm a plan.

        Returns a redacted result; the access token and derived API key are never
        returned or logged. ``exchange`` is injectable for tests.
        """
        token = self._read_access_token(self.active_token_dir(paths))
        if not token:
            return Entitlement(ok=False, detail="no GitHub Copilot credential found")
        do_exchange = exchange or self._exchange_access_token
        try:
            status, body = do_exchange(token)
        except Exception as exc:  # noqa: BLE001 - any transport failure is inconclusive
            return Entitlement(
                ok=False, detail=f"could not reach GitHub Copilot ({type(exc).__name__})"
            )
        if status == 200 and isinstance(body, dict) and body.get("token"):
            return Entitlement(ok=True, detail="active Copilot subscription")
        return Entitlement(ok=False, detail=_entitlement_detail(status, body))

    @staticmethod
    def _read_access_token(token_dir: Path) -> str:
        try:
            return read_text(token_dir / _ACCESS_TOKEN_FILE).strip()
        except OSError:
            return ""

    @staticmethod
    def _exchange_access_token(token: str) -> tuple[int, Any]:
        headers = {**_COPILOT_GITHUB_HEADERS, "authorization": f"token {token}"}
        with httpx.Client(timeout=_ENTITLEMENT_TIMEOUT) as client:
            resp = client.get(_copilot_api_key_url(), headers=headers)
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, resp.text
