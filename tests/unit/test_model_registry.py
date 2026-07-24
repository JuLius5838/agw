"""Unit tests for model registry parsing and validation invariants."""

from __future__ import annotations

import json
import textwrap

import pytest

from agent_gateway.errors import ConfigError, ModelUnavailableError
from agent_gateway.model_registry import (
    ModelMode,
    add_model_to_registry_text,
    load_default_registry,
    load_registry_text,
    remove_model_from_registry_text,
)
from agent_gateway.providers import Provider

VALID = textwrap.dedent(
    """
    default_model: gpt-5.3-codex
    models:
      - name: gpt-5.3-codex
        provider: chatgpt
        upstream_model: chatgpt/gpt-5.3-codex
        mode: responses
        enabled: true
      - name: gpt-5.6-terra
        provider: chatgpt
        upstream_model: chatgpt/gpt-5.6-terra
        mode: responses
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
    assert [m.name for m in reg.inactive_models()] == ["gpt-5.6-terra"]


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
                provider: chatgpt
                upstream_model: chatgpt/gpt-5.3-codex-alt
                mode: responses
                enabled: false
            """
        )
    )
    default = reg.default_entry()
    assert default is not None
    assert default.provider.value == "chatgpt"


def test_duplicate_active_names_rejected():
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
            provider: chatgpt
            upstream_model: chatgpt/gpt-5.3-codex-alt
            mode: responses
            enabled: true
        """
    )
    with pytest.raises(ConfigError) as excinfo:
        load_registry_text(text)
    message = str(excinfo.value)
    assert "chatgpt" in message


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
          - name: gpt-5.6-terra
            provider: chatgpt
            upstream_model: chatgpt/gpt-5.6-terra
            mode: responses
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
            upstream_model: openai/gpt-5.3-codex
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
          - name: gpt-5.6-terra
            provider: chatgpt
            upstream_model: chatgpt/gpt-5.6-terra
            mode: responses
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
    with pytest.raises(ModelUnavailableError, match="chatgpt"):
        reg.get_active("gpt-5.6-terra")


def test_packaged_default_registry_is_valid():
    reg = load_default_registry()
    assert reg.default_entry() is None
    assert not reg.active_models()
    assert {entry.name for entry in reg.inactive_models()} == {
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    }
    assert {entry.name: entry.display_name for entry in reg.inactive_models()} == {
        "gpt-5.6-sol": "GPT 5.6 Sol",
        "gpt-5.6-terra": "GPT 5.6 Terra",
        "gpt-5.6-luna": "GPT 5.6 Luna",
    }


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


# --------------------------------------------------------------------------- #
# add_model_to_registry_text
# --------------------------------------------------------------------------- #
_ONE_ACTIVE = textwrap.dedent(
    """
    default_model: null
    models:
      - name: gpt-5.6-sol
        provider: chatgpt
        upstream_model: chatgpt/gpt-5.6-sol
        mode: responses
        enabled: true
    """
)


def test_add_model_appends_and_defaults_upstream():
    text = add_model_to_registry_text(
        _ONE_ACTIVE,
        name="gpt-5.6-luna",
        provider=Provider.chatgpt,
        upstream_model="chatgpt/gpt-5.6-luna",
        mode=ModelMode.responses,
        display_name="GPT 5.6 Luna",
    )
    reg = load_registry_text(text)
    names = {m.name for m in reg.active_models()}
    assert names == {"gpt-5.6-sol", "gpt-5.6-luna"}
    luna = reg.get_active("gpt-5.6-luna")
    assert luna.upstream_model == "chatgpt/gpt-5.6-luna"
    assert luna.display_name == "GPT 5.6 Luna"


def test_add_model_onto_empty_registry():
    text = add_model_to_registry_text(
        "default_model: null\nmodels: []\n",
        name="gpt-5.6-terra",
        provider=Provider.chatgpt,
        upstream_model="chatgpt/gpt-5.6-terra",
        mode=ModelMode.responses,
    )
    reg = load_registry_text(text)
    assert [m.name for m in reg.active_models()] == ["gpt-5.6-terra"]


def test_add_model_disabled_candidate():
    text = add_model_to_registry_text(
        _ONE_ACTIVE,
        name="gpt-5.6-luna",
        provider=Provider.chatgpt,
        upstream_model="chatgpt/gpt-5.6-luna",
        mode=ModelMode.responses,
        enabled=False,
    )
    reg = load_registry_text(text)
    assert [m.name for m in reg.active_models()] == ["gpt-5.6-sol"]
    assert "gpt-5.6-luna" in {m.name for m in reg.inactive_models()}


def test_add_model_make_default_sets_startup_model():
    text = add_model_to_registry_text(
        _ONE_ACTIVE,
        name="gpt-5.6-luna",
        provider=Provider.chatgpt,
        upstream_model="chatgpt/gpt-5.6-luna",
        mode=ModelMode.responses,
        make_default=True,
    )
    reg = load_registry_text(text)
    assert reg.default_model == "gpt-5.6-luna"


def test_add_model_duplicate_name_and_provider_rejected():
    with pytest.raises(ConfigError, match="already exists"):
        add_model_to_registry_text(
            _ONE_ACTIVE,
            name="gpt-5.6-sol",
            provider=Provider.chatgpt,
            upstream_model="chatgpt/gpt-5.6-sol",
            mode=ModelMode.responses,
        )


def test_add_model_invalid_prefix_rejected_without_writing():
    with pytest.raises(ConfigError):
        add_model_to_registry_text(
            _ONE_ACTIVE,
            name="gpt-5.6-luna",
            provider=Provider.chatgpt,
            upstream_model="openai/gpt-5.6-luna",
            mode=ModelMode.responses,
        )


def test_add_model_native_name_rejected():
    with pytest.raises(ConfigError):
        add_model_to_registry_text(
            _ONE_ACTIVE,
            name="claude-opus-4-8",
            provider=Provider.chatgpt,
            upstream_model="chatgpt/claude-opus-4-8",
            mode=ModelMode.responses,
        )


# --------------------------------------------------------------------------- #
# remove_model_from_registry_text
# --------------------------------------------------------------------------- #
def test_remove_model_drops_entry():
    two = add_model_to_registry_text(
        _ONE_ACTIVE,
        name="gpt-5.6-luna",
        provider=Provider.chatgpt,
        upstream_model="chatgpt/gpt-5.6-luna",
        mode=ModelMode.responses,
    )
    text = remove_model_from_registry_text(two, name="gpt-5.6-luna")
    reg = load_registry_text(text)
    assert [m.name for m in reg.active_models()] == ["gpt-5.6-sol"]


def test_remove_model_clears_default_when_it_no_longer_resolves():
    text = remove_model_from_registry_text(
        _ONE_ACTIVE.replace("default_model: null", "default_model: gpt-5.6-sol"),
        name="gpt-5.6-sol",
    )
    reg = load_registry_text(text)
    assert reg.default_model is None
    assert reg.active_models() == ()


def test_remove_model_unknown_raises():
    with pytest.raises(ModelUnavailableError, match="not configured"):
        remove_model_from_registry_text(_ONE_ACTIVE, name="does-not-exist")


def test_remove_model_ambiguous_across_providers_requires_provider():
    # Raw registry with the same name under two providers. The ambiguity guard
    # runs on the raw document before the final whole-registry validation, so it
    # does not depend on both providers being current enum members.
    text = textwrap.dedent(
        """
        default_model: null
        models:
          - name: shared
            provider: chatgpt
            upstream_model: chatgpt/shared
            mode: responses
            enabled: false
          - name: shared
            provider: copilot
            upstream_model: github_copilot/shared
            mode: chat
            enabled: false
        """
    )
    with pytest.raises(ConfigError, match="multiple provider candidates"):
        remove_model_from_registry_text(text, name="shared")


def test_remove_model_removes_all_same_name_entries_for_one_provider():
    text = textwrap.dedent(
        """
        default_model: null
        models:
          - name: shared
            provider: chatgpt
            upstream_model: chatgpt/shared
            mode: responses
            enabled: true
          - name: shared
            provider: chatgpt
            upstream_model: chatgpt/shared-alt
            mode: responses
            enabled: false
        """
    )
    result = remove_model_from_registry_text(text, name="shared")
    reg = load_registry_text(result)
    assert reg.active_models() == ()
    assert reg.inactive_models() == ()
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
