"""Legacy migration marker for the removed Anthropic adapter.

AGW never mints, stores, or injects a separate Anthropic token. Claude Code keeps
its own saved claude.ai login, and the front router forwards that request-time
credential directly to Anthropic. This module remains temporarily so upgrades
from early development builds fail with a clear explanation instead of importing
the old token-minting implementation.
"""

from __future__ import annotations

from agent_gateway.errors import AuthError


def unsupported_anthropic_auth() -> None:
    """Explain why ``agw auth anthropic`` is intentionally unsupported."""
    raise AuthError(
        "Anthropic authentication is owned by the native Claude Code login.",
        hint="Run `claude` normally and complete Claude Code's sign-in if prompted.",
    )
