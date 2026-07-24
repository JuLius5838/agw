"""Unit tests for model registry parsing and validation invariants."""

from __future__ import annotations

import json
import textwrap

import pytest

from agent_gateway.errors import ConfigError, ModelUnavailableError
from agent_gateway.model_registry import load_default_registry, load_registry_text

VALID = textwrap.dedent(
    """
    default_model: gpt-5.3-codex
    models:
      - name: gpt-5.3-codex
        provider: chatgpt
        upstream_model: chatgpt/gpt-5.3-codex
        mode: responses
        enabled: true
      - name: gpt-4.1
        provider: copilot
        upstream_model: github_copilot/gpt-4.1
        mode: chat
        enabled: false
    """
)


def test_valid_registry_loads():
    reg = load_registry_text(VALID)
    assert reg.default_model == "gpt-5.3-codex"
    active = reg.active_models()
    assert [m.name for m in active] == ["gpt-5.3-codex"]
    default = reg.default_entry()
    assert default is not None
    assert default.provider.value == "chatgpt"
    assert [m.name for m in reg.inactive_models()] == ["gpt-4.1"]


def test_inactive_duplicate_candidate_is_accepted():
    # Same public name may appear twice as long as at most one is active.
    reg = load_registry_text(
        textwrap.dedent(
            """
            default_model: gpt-5.3-codex
            models:
              - name: gpt-5.3-codex
                provider: chatgpt
                upstream_model: chatgpt/gpt-5.3-codex
                mode: responses
                enabled: true
              - name: gpt-5.3-codex
                provider: copilot
                upstream_model: github_copilot/gpt-5.3-codex
                mode: chat
                enabled: false
            """
        )
    )
    default = reg.default_entry()
    assert default is not None
    assert default.provider.value == "chatgpt"


def test_duplicate_active_names_rejected_and_names_both_providers():
    text = textwrap.dedent(
        """
        default_model: gpt-5.3-codex
        models:
          - name: gpt-5.3-codex
            provider: chatgpt
            upstream_model: chatgpt/gpt-5.3-codex
            mode: responses
            enabled: true
          - name: gpt-5.3-codex
            provider: copilot
            upstream_model: github_copilot/gpt-5.3-codex
            mode: chat
            enabled: true
        """
    )
    with pytest.raises(ConfigError) as excinfo:
        load_registry_text(text)
    message = str(excinfo.value)
    assert "chatgpt" in message and "copilot" in message


def test_default_disabled_rejected():
    # default points at a disabled entry while another entry is active.
    text = textwrap.dedent(
        """
        default_model: gpt-5.3-codex
        models:
          - name: gpt-5.3-codex
            provider: chatgpt
            upstream_model: chatgpt/gpt-5.3-codex
            mode: responses
            enabled: false
          - name: gpt-4.1
            provider: copilot
            upstream_model: github_copilot/gpt-4.1
            mode: chat
            enabled: true
        """
    )
    with pytest.raises(ConfigError, match="not active"):
        load_registry_text(text)


def test_default_unknown_rejected():
    text = textwrap.dedent(
        """
        default_model: nonexistent
        models:
          - name: gpt-5.3-codex
            provider: chatgpt
            upstream_model: chatgpt/gpt-5.3-codex
            mode: responses
            enabled: true
        """
    )
    with pytest.raises(ConfigError, match="not a configured model"):
        load_registry_text(text)


def test_invalid_prefix_rejected():
    text = textwrap.dedent(
        """
        default_model: gpt-5.3-codex
        models:
          - name: gpt-5.3-codex
            provider: chatgpt
            upstream_model: github_copilot/gpt-5.3-codex
            mode: responses
            enabled: true
        """
    )
    with pytest.raises(ConfigError, match="must start with"):
        load_registry_text(text)


def test_invalid_mode_rejected():
    text = textwrap.dedent(
        """
        default_model: gpt-5.3-codex
        models:
          - name: gpt-5.3-codex
            provider: chatgpt
            upstream_model: chatgpt/gpt-5.3-codex
            mode: streaming
            enabled: true
        """
    )
    with pytest.raises(ConfigError):
        load_registry_text(text)


def test_public_name_with_prefix_rejected():
    text = textwrap.dedent(
        """
        default_model: chatgpt/gpt-5.3-codex
        models:
          - name: chatgpt/gpt-5.3-codex
            provider: chatgpt
            upstream_model: chatgpt/gpt-5.3-codex
            mode: responses
            enabled: true
        """
    )
    with pytest.raises(ConfigError, match="provider prefix"):
        load_registry_text(text)


def test_empty_active_registry_allowed_for_native_claude():
    text = textwrap.dedent(
        """
        default_model: gpt-5.3-codex
        models:
          - name: gpt-5.3-codex
            provider: chatgpt
            upstream_model: chatgpt/gpt-5.3-codex
            mode: responses
            enabled: false
          - name: gpt-4.1
            provider: copilot
            upstream_model: github_copilot/gpt-4.1
            mode: chat
            enabled: false
        """
    )
    text = text.replace("default_model: gpt-5.3-codex", "default_model: null")
    reg = load_registry_text(text)
    assert reg.active_models() == ()
    assert reg.default_entry() is None


def test_get_active_unknown_raises_model_unavailable():
    reg = load_registry_text(VALID)
    with pytest.raises(ModelUnavailableError):
        reg.get_active("does-not-exist")


def test_get_active_inactive_raises_model_unavailable_with_provider():
    reg = load_registry_text(VALID)
    with pytest.raises(ModelUnavailableError, match="copilot"):
        reg.get_active("gpt-4.1")


def test_packaged_default_registry_is_valid():
    reg = load_default_registry()
    assert reg.default_entry() is None
    assert not reg.active_models()


def test_native_claude_is_not_a_registry_provider():
    with pytest.raises(ConfigError):
        load_registry_text(
            textwrap.dedent(
                """
                default_model: null
                models:
                  - name: claude-opus-4-8
                    provider: anthropic
                    upstream_model: anthropic/claude-opus-4-8
                    mode: chat
                    enabled: true
                """
            )
        )


def test_native_claude_name_is_rejected_even_with_external_provider():
    text = textwrap.dedent(
        """
        default_model: claude-opus-4-8
        models:
          - name: claude-opus-4-8
            provider: chatgpt
            upstream_model: chatgpt/claude-opus-4-8
            mode: chat
            enabled: true
        """
    )
    with pytest.raises(ConfigError):
        load_registry_text(text)


def test_picker_ids_are_hidden_but_display_name_is_exact():
    reg = load_registry_text(
        """
        default_model: gpt-5.6-sol
        models:
          - name: gpt-5.6-sol
            provider: chatgpt
            upstream_model: chatgpt/gpt-5.6-sol
            mode: responses
            enabled: true
        """
    )
    assert reg.picker_models() == (("anthropic.agw.chatgpt.gpt-5.6-sol", "gpt-5.6-sol"),)
    assert reg.resolve_routed_model("gpt-5.6-sol") is not None
    assert reg.resolve_routed_model("anthropic.agw.chatgpt.gpt-5.6-sol") is not None


def test_picker_display_name_is_independent_from_routing_name():
    reg = load_registry_text(
        """
        default_model: gpt-5.6-sol
        models:
          - name: gpt-5.6-sol
            display_name: Codex Sol
            provider: chatgpt
            upstream_model: chatgpt/gpt-5.6-sol
            mode: responses
            enabled: true
        """
    )
    assert reg.picker_models() == (("anthropic.agw.chatgpt.gpt-5.6-sol", "Codex Sol"),)
    assert reg.get_active("gpt-5.6-sol").display_name == "Codex Sol"
    assert reg.resolve_routed_model("Codex Sol") is None


@pytest.mark.parametrize("display_name", ["", " padded", "padded ", "line\nbreak"])
def test_invalid_picker_display_name_rejected(display_name: str):
    text = VALID.replace(
        "provider: chatgpt",
        f"display_name: {json.dumps(display_name)}\n    provider: chatgpt",
        1,
    )
    with pytest.raises(ConfigError, match="display_name"):
        load_registry_text(text)
