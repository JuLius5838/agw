"""Unit tests for the Claude harness environment contract (FR-21)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from agent_gateway.config import EffortLevel, GatewayConfig
from agent_gateway.harnesses.claude import ClaudeHarness, _launch_args
from agent_gateway.model_registry import load_registry_text

REGISTRY = load_registry_text(
    """
    default_model: gpt-5.3-codex
    models:
      - name: gpt-5.3-codex
        provider: chatgpt
        upstream_model: chatgpt/gpt-5.3-codex
        mode: responses
        enabled: true
    """
)

FAMILY_VARS = (
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
)


def _env(
    base: Mapping[str, str] | None = None,
    args: Sequence[str] = (),
    *,
    agent_teams: bool = False,
    port: int = 4000,
    default_effort: EffortLevel | None = None,
) -> dict[str, str]:
    config = GatewayConfig(
        port=port,
        native_claude_path="/usr/bin/claude",
        agent_teams_enabled=agent_teams,
        default_effort=default_effort,
    )
    return ClaudeHarness().build_env(dict(base or {}), config, REGISTRY, "sk-agw-key", list(args))


def test_sets_gateway_endpoint_and_local_custom_header():
    env = _env(port=4123)
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4123"
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == "X-AGW-Key: sk-agw-key"
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] == "1"
    assert env["ANTHROPIC_CUSTOM_MODEL_OPTION"] == "gpt-5.3-codex"
    assert env["ANTHROPIC_CUSTOM_MODEL_OPTION_NAME"] == "gpt-5.3-codex"
    assert "Agent Gateway" in env["ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION"]


def test_custom_picker_uses_configured_display_name():
    registry = load_registry_text(
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
    env = ClaudeHarness().build_env({}, GatewayConfig(), registry, "sk-agw-test", ())
    assert env["ANTHROPIC_CUSTOM_MODEL_OPTION"] == "gpt-5.6-sol"
    assert env["ANTHROPIC_CUSTOM_MODEL_OPTION_NAME"] == "Codex Sol"


def test_gateway_picker_fallback_replaces_stale_custom_option():
    env = _env(
        base={
            "ANTHROPIC_CUSTOM_MODEL_OPTION": "stale-model",
            "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": "Stale",
            "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION": "Stale custom option",
        }
    )
    assert env["ANTHROPIC_CUSTOM_MODEL_OPTION"] == "gpt-5.3-codex"


def test_default_model_applied_when_no_explicit_model():
    assert _env()["ANTHROPIC_MODEL"] == "gpt-5.3-codex"


def test_explicit_model_flag_suppresses_anthropic_model():
    assert "ANTHROPIC_MODEL" not in _env(args=["--model", "gpt-5.3-codex"])
    assert "ANTHROPIC_MODEL" not in _env(args=["--model=whatever"])


def test_native_family_defaults_are_preserved():
    env = _env(args=["--model", "gpt-5.3-codex"])
    for var in FAMILY_VARS:
        assert var not in env


def test_strips_subagent_override_and_cloud_flags_but_preserves_others():
    base = {
        "CLAUDE_CODE_SUBAGENT_MODEL": "sneaky",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_VERTEX": "1",
        "CLAUDE_CODE_USE_FOUNDRY": "1",
        "UNRELATED_VAR": "keep-me",
    }
    env = _env(base=base)
    assert "CLAUDE_CODE_SUBAGENT_MODEL" not in env
    assert "CLAUDE_CODE_USE_BEDROCK" not in env
    assert "CLAUDE_CODE_USE_VERTEX" not in env
    assert "CLAUDE_CODE_USE_FOUNDRY" not in env
    assert env["UNRELATED_VAR"] == "keep-me"


def test_agent_teams_opt_in_controls_flag():
    assert _env(agent_teams=True)["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] == "1"
    assert "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS" not in _env(agent_teams=False)


def test_provider_token_dirs_never_leak_into_child_env():
    env = _env()
    assert "CHATGPT_TOKEN_DIR" not in env
    assert "GITHUB_COPILOT_TOKEN_DIR" not in env


def test_inherited_anthropic_model_removed_when_explicit_flag_given():
    env = _env(base={"ANTHROPIC_MODEL": "opus"}, args=["--model", "x"])
    assert "ANTHROPIC_MODEL" not in env


def test_inherited_anthropic_model_replaced_by_default_without_flag():
    env = _env(base={"ANTHROPIC_MODEL": "opus"})
    assert env["ANTHROPIC_MODEL"] == "gpt-5.3-codex"


def test_configured_effort_default_is_injected_as_overridable_flag():
    config = GatewayConfig(default_effort="max")
    assert _launch_args(config, ["chat"]) == ["--effort", "max", "chat"]


def test_explicit_effort_wins_over_configured_default():
    config = GatewayConfig(default_effort="max")
    assert _launch_args(config, ["--effort", "low", "chat"]) == [
        "--effort",
        "low",
        "chat",
    ]
    assert _launch_args(config, ["--effort=xhigh", "chat"]) == [
        "--effort=xhigh",
        "chat",
    ]
    assert _launch_args(config, ["--", "--effort", "low"]) == [
        "--effort",
        "max",
        "--",
        "--effort",
        "low",
    ]


def test_configured_effort_removes_inherited_hard_override():
    env = _env(
        base={"CLAUDE_CODE_EFFORT_LEVEL": "low"},
        default_effort="max",
    )
    assert "CLAUDE_CODE_EFFORT_LEVEL" not in env


def test_inherited_effort_is_preserved_without_agw_default():
    env = _env(base={"CLAUDE_CODE_EFFORT_LEVEL": "low"})
    assert env["CLAUDE_CODE_EFFORT_LEVEL"] == "low"
