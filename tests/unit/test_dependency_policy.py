"""Dependency-policy guardrails.

Anthropic's gateway documentation identifies LiteLLM ``1.82.7`` and ``1.82.8`` as
compromised. These must never be the installed or locked version.
"""

from __future__ import annotations

from importlib.metadata import version
from pathlib import Path

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
