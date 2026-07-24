"""Dependency-policy guardrails.

Anthropic's gateway documentation identifies LiteLLM ``1.82.7`` and ``1.82.8`` as
compromised. These must never be the installed or locked version.
"""

from __future__ import annotations

import inspect
from importlib.metadata import version
from pathlib import Path

from litellm import model_cost
from litellm.llms.github_copilot.authenticator import Authenticator

FORBIDDEN_LITELLM = {"1.82.7", "1.82.8"}
REPO_ROOT = Path(__file__).resolve().parents[2]


def test_installed_litellm_is_not_compromised() -> None:
    assert version("litellm") not in FORBIDDEN_LITELLM


def test_lockfile_pins_no_forbidden_litellm() -> None:
    lock = (REPO_ROOT / "uv.lock").read_text()
    for bad in FORBIDDEN_LITELLM:
        assert f'version = "{bad}"' not in lock, f"lockfile pins forbidden litellm {bad}"


def test_pyproject_pins_litellm_with_proxy_extra() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    assert "litellm[proxy]==" in pyproject  # pinned, exact, with proxy extra


def test_pinned_litellm_exposes_expected_copilot_contract() -> None:
    assert version("litellm") == "1.93.0"
    source = inspect.getsource(Authenticator.__init__)
    assert "GITHUB_COPILOT_TOKEN_DIR" in source
    assert model_cost["github_copilot/gpt-4.1"]["mode"] == "chat"
    # A product display name is not enough to route; never silently add a guessed Kimi slug.
    assert not any(key.startswith("github_copilot/kimi") for key in model_cost)


def test_copilot_entitlement_endpoint_matches_pinned_litellm() -> None:
    # The entitlement diagnostic reads this URL from LiteLLM to avoid drift.
    from litellm.llms.github_copilot.authenticator import DEFAULT_GITHUB_API_KEY_URL

    from agent_gateway.providers.copilot import _copilot_api_key_url

    assert _copilot_api_key_url() == DEFAULT_GITHUB_API_KEY_URL
    assert DEFAULT_GITHUB_API_KEY_URL.endswith("/copilot_internal/v2/token")
