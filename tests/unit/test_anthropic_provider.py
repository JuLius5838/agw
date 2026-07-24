"""Migration behavior for the intentionally removed Anthropic adapter."""

import pytest

from agent_gateway.errors import AuthError
from agent_gateway.providers.anthropic import unsupported_anthropic_auth


def test_legacy_anthropic_auth_points_to_native_claude_login() -> None:
    with pytest.raises(AuthError, match="native Claude Code login") as excinfo:
        unsupported_anthropic_auth()

    assert "Run `claude` normally" in (excinfo.value.hint or "")
