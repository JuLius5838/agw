"""Unit tests for deterministic LiteLLM config rendering."""

from __future__ import annotations

import textwrap

import yaml

from agent_gateway.litellm_config import (
    config_fingerprint,
    render_litellm_config,
)
from agent_gateway.model_registry import load_registry_text

REGISTRY = textwrap.dedent(
    """
    default_model: gpt-5.3-codex
    models:
      - name: zeta-model
        provider: chatgpt
        upstream_model: chatgpt/zeta-model
        mode: responses
        enabled: true
      - name: gpt-5.3-codex
        provider: chatgpt
        upstream_model: chatgpt/gpt-5.3-codex
        mode: responses
        enabled: true
      - name: inactive-candidate
        provider: chatgpt
        upstream_model: chatgpt/inactive-candidate
        mode: responses
        enabled: false
    """
)


def test_render_is_deterministic():
    reg = load_registry_text(REGISTRY)
    assert render_litellm_config(reg) == render_litellm_config(reg)


def test_render_uses_public_name_and_prefixed_upstream():
    reg = load_registry_text(REGISTRY)
    doc = yaml.safe_load(render_litellm_config(reg))
    by_name = {m["model_name"]: m for m in doc["model_list"]}
    assert by_name["gpt-5.3-codex"]["litellm_params"]["model"] == "chatgpt/gpt-5.3-codex"
    assert by_name["gpt-5.3-codex"]["model_info"]["mode"] == "responses"


def test_copilot_route_keeps_exact_public_name_and_private_prefix():
    reg = load_registry_text(
        """
        default_model: gpt-4.1
        models:
          - name: gpt-4.1
            provider: copilot
            upstream_model: github_copilot/gpt-4.1
            mode: chat
            enabled: true
        """
    )
    doc = yaml.safe_load(render_litellm_config(reg))
    assert doc["model_list"] == [
        {
            "model_name": "gpt-4.1",
            "litellm_params": {"model": "github_copilot/gpt-4.1"},
            "model_info": {"mode": "chat"},
        }
    ]


def test_only_active_models_rendered_sorted_by_name():
    reg = load_registry_text(REGISTRY)
    doc = yaml.safe_load(render_litellm_config(reg))
    names = [m["model_name"] for m in doc["model_list"]]
    # inactive-candidate excluded; active names sorted.
    assert names == ["gpt-5.3-codex", "zeta-model"]


def test_no_prefix_leaks_into_public_model_name():
    reg = load_registry_text(REGISTRY)
    doc = yaml.safe_load(render_litellm_config(reg))
    for entry in doc["model_list"]:
        assert "/" not in entry["model_name"]


def test_no_secret_material_in_output():
    reg = load_registry_text(REGISTRY)
    rendered = render_litellm_config(reg)
    lowered = rendered.lower()
    for forbidden in ("master_key", "sk-", "api_key", "authorization", "token"):
        assert forbidden not in lowered


def test_message_logging_is_disabled():
    reg = load_registry_text(REGISTRY)
    doc = yaml.safe_load(render_litellm_config(reg))
    assert doc["litellm_settings"]["turn_off_message_logging"] is True


def test_fingerprint_stable_and_content_sensitive():
    reg = load_registry_text(REGISTRY)
    rendered = render_litellm_config(reg)
    assert config_fingerprint(rendered) == config_fingerprint(rendered)
    assert config_fingerprint(rendered) != config_fingerprint(rendered + "\n# drift")
