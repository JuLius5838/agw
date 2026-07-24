"""The provider adapter interface.

An adapter isolates everything provider-specific behind a small surface:
  * the process environment that points LiteLLM (and the device flow) at a token
    directory,
  * running the provider's interactive OAuth device flow into a directory,
  * a bounded structural probe of freshly staged credentials,
  * inspecting the *active* credentials for ``doctor``/``status``, and
  * the remediation command to print on failure.

Adapters never print or log tokens. The only thing that may reach the TTY is the
short-lived verification URL/code that the provider's own device flow prints.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from agent_gateway.paths import Paths
from agent_gateway.providers import Provider


class AuthStatus(StrEnum):
    """Coarse authentication state for a provider's active credentials."""

    authenticated = "authenticated"
    expired = "expired"
    missing = "missing"


@dataclass(frozen=True)
class AuthState:
    """A redacted snapshot of a provider's auth state (never contains secrets)."""

    status: AuthStatus
    detail: str


class ProviderAdapter(ABC):
    """Base class for provider authentication adapters."""

    provider: Provider
    display_name: str

    def active_token_dir(self, paths: Paths) -> Path:
        """The provider's active credential directory (isolated per provider)."""
        return paths.provider_credentials_dir(self.provider.value)

    @abstractmethod
    def process_env(self, token_dir: Path) -> dict[str, str]:
        """Environment variables that point the provider's tooling at ``token_dir``."""

    @abstractmethod
    def run_device_flow(self, token_dir: Path, *, model: str | None = None) -> None:
        """Run the interactive OAuth device flow, writing tokens into ``token_dir``.

        Must raise :class:`agent_gateway.errors.AuthError` on failure/cancellation.
        """

    @abstractmethod
    def probe_staged(self, token_dir: Path) -> None:
        """Bounded structural validation of freshly staged credentials.

        Raise :class:`agent_gateway.errors.AuthError` if the staged credentials
        are missing or unusable, so a broken credential is never swapped in.
        """

    @abstractmethod
    def auth_state(self, paths: Paths) -> AuthState:
        """Inspect the active credentials (for ``doctor``/``status``)."""

    def remediation(self) -> str:
        """The command a user runs to (re)authenticate this provider."""
        return f"agw auth {self.provider.value}"
