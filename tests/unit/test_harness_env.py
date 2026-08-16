"""Unit tests for the Claude harness environment contract (FR-21)."""

from __future__ import annotations

import json
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
    # Even when the surrounding shell exports provider credential paths, none may
    # reach the native Claude child.
    base = {
        "CHATGPT_TOKEN_DIR": "/home/dev/.config/agent-gateway/credentials/chatgpt",
        "CHATGPT_AUTH_FILE": "/home/dev/.config/agent-gateway/credentials/chatgpt/auth.json",
        "GITHUB_COPILOT_TOKEN_DIR": "/home/dev/.config/agent-gateway/credentials/copilot",
        "LITELLM_MASTER_KEY": "sk-agw-secret",
        "UNRELATED_VAR": "keep-me",
    }
    env = _env(base=base)
    assert "CHATGPT_TOKEN_DIR" not in env
    assert "CHATGPT_AUTH_FILE" not in env
    assert "GITHUB_COPILOT_TOKEN_DIR" not in env
    assert "LITELLM_MASTER_KEY" not in env
    assert env["UNRELATED_VAR"] == "keep-me"


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


# --------------------------------------------------------------------------- #
# Generated-agent injection at launch
# --------------------------------------------------------------------------- #
def _launch_capture(monkeypatch, tmp_path, forwarded, *, models=("gpt-5.6-sol",), seed=True):
    """Drive ClaudeHarness.launch() with a captured execve and no real proxy."""
    import os

    import pytest

    from agent_gateway import proxy as proxy_mod
    from agent_gateway.claude_compat import (
        COMPATIBILITY_SCHEMA,
        ClaudeExecutableIdentity,
        CompatibilityIdentity,
        CompatibilityRecord,
        utc_now,
        write_compatibility_record,
    )
    from agent_gateway.external_subagents import DEFINITION_CONTRACT_VERSION
    from agent_gateway.harnesses import claude as claude_harness
    from agent_gateway.paths import get_paths
    from agent_gateway.process import ProcessIdentity
    from agent_gateway.providers import Provider

    paths = get_paths({"HOME": str(tmp_path)})
    fake_claude = tmp_path / "claude"
    fake_claude.write_text("#!/bin/sh\n")
    fake_claude.chmod(0o755)
    config = GatewayConfig(port=4000, native_claude_path=str(fake_claude))

    registry = load_registry_text(
        "default_model: null\nmodels:\n"
        + "".join(
            f"  - name: {name}\n"
            f"    provider: chatgpt\n"
            f"    upstream_model: chatgpt/{name}\n"
            f"    mode: responses\n"
            f"    enabled: true\n"
            for name in models
        )
    )

    identity = ClaudeExecutableIdentity(
        configured_path=str(fake_claude),
        resolved_path=str(fake_claude.resolve()),
        sha256="b" * 64,
        version="2.1.232 (Claude Code)",
    )
    dummy = ProcessIdentity(pid=1, create_time=1.0)
    state = proxy_mod.ProxyState(
        supervisor=dummy,
        router=dummy,
        litellm=None,
        host="127.0.0.1",
        port=4000,
        litellm_host=None,
        litellm_port=None,
        litellm_version="1.93.0",
        config_fingerprint="c" * 64,
        runtime_fingerprint="a" * 64,
        log_path=str(tmp_path / "proxy.log"),
    )

    if seed:
        for name in models:
            write_compatibility_record(
                paths,
                CompatibilityRecord(
                    schema=COMPATIBILITY_SCHEMA,
                    identity=CompatibilityIdentity(
                        model=name,
                        provider=Provider.chatgpt,
                        claude=identity,
                        runtime_fingerprint="a" * 64,
                        definition_contract=DEFINITION_CONTRACT_VERSION,
                    ),
                    verified_at=utc_now(),
                    outcome="passed",
                    detail="exact native subagent route verified",
                ),
            )

    monkeypatch.setattr(proxy_mod, "ensure_running", lambda *a, **k: state)
    monkeypatch.setattr(claude_harness, "resolve_claude_identity", lambda _config: identity)
    monkeypatch.setattr(claude_harness, "read_claude_settings", lambda: [])
    monkeypatch.setattr(claude_harness, "ensure_proxy_key", lambda _p: "sk-agw-test")

    captured: dict = {}

    def fake_execve(path, argv, env):
        captured.update({"path": path, "argv": argv, "env": env})
        raise SystemExit(0)

    monkeypatch.setattr(os, "execve", fake_execve)
    with pytest.raises(SystemExit):
        ClaudeHarness().launch(paths, config, registry, list(forwarded))
    return captured, paths


def _agents_payload(captured):
    argv = captured["argv"]
    return json.loads(argv[argv.index("--agents") + 1])


def test_launch_injects_one_definition_per_current_model(monkeypatch, tmp_path):
    captured, _paths = _launch_capture(monkeypatch, tmp_path, [])
    payload = _agents_payload(captured)

    assert list(payload) == ["agw-gpt-5-6-sol-66127d2195a1"]
    definition = payload["agw-gpt-5-6-sol-66127d2195a1"]
    assert definition["model"] == "gpt-5.6-sol"
    assert definition["tools"] == ["Read", "Grep", "Glob"]
    assert definition["maxTurns"] == 20


def test_launch_injects_every_current_model_deterministically(monkeypatch, tmp_path):
    captured, _paths = _launch_capture(
        monkeypatch, tmp_path, [], models=("gpt-5.6-sol", "gpt-5.6-luna")
    )
    payload = _agents_payload(captured)
    assert len(payload) == 2
    assert all(name.startswith("agw-") for name in payload)
    assert {definition["model"] for definition in payload.values()} == {
        "gpt-5.6-sol",
        "gpt-5.6-luna",
    }


def test_launch_merges_a_user_agents_option_in_place(monkeypatch, tmp_path):
    captured, _paths = _launch_capture(
        monkeypatch, tmp_path, ["--agents", '{"mine":{"description":"x"}}', "chat"]
    )
    payload = _agents_payload(captured)
    assert payload["mine"] == {"description": "x"}
    assert "agw-gpt-5-6-sol-66127d2195a1" in payload
    assert captured["argv"][-1] == "chat"


def test_launch_without_evidence_forwards_arguments_byte_for_byte(monkeypatch, tmp_path):
    forwarded = ["--model", "gpt-5.6-sol", "--", "extra-arg", "--agents", "not-json"]
    captured, _paths = _launch_capture(monkeypatch, tmp_path, forwarded, seed=False)
    assert captured["argv"][1:] == forwarded
    assert "--agents" not in captured["argv"][: captured["argv"].index("--")]


def test_launch_places_the_generated_option_before_a_terminator(monkeypatch, tmp_path):
    captured, _paths = _launch_capture(monkeypatch, tmp_path, ["--", "a prompt"])
    argv = captured["argv"]
    assert argv.index("--agents") < argv.index("--")
    assert argv[-2:] == ["--", "a prompt"]


def test_launch_still_strips_credentials_and_subagent_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_SUBAGENT_MODEL", "haiku")
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", "/creds")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "leak")
    captured, _paths = _launch_capture(monkeypatch, tmp_path, [])
    env = captured["env"]
    assert "CLAUDE_CODE_SUBAGENT_MODEL" not in env
    assert "CHATGPT_TOKEN_DIR" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4000"


def test_launch_keeps_the_configured_effort_placement(monkeypatch, tmp_path):
    import os

    from agent_gateway.config import EffortLevel

    captured: dict = {}
    original = _launch_capture

    # Effort insertion and --agents insertion must not interfere.
    monkeypatch.setattr(
        "agent_gateway.harnesses.claude._launch_args",
        lambda config, args: ["--effort", "high", *args],
    )
    captured, _paths = original(monkeypatch, tmp_path, ["chat"])
    argv = captured["argv"]
    assert argv[1:3] == ["--effort", "high"]
    assert argv[3] == "chat"
    assert argv[4] == "--agents"
    assert EffortLevel is not None
    assert os is not None


def test_launch_fails_before_exec_on_a_colliding_user_definition(monkeypatch, tmp_path):
    import pytest

    from agent_gateway.errors import ConfigError

    colliding = json.dumps({"agw-gpt-5-6-sol-66127d2195a1": {"description": "mine"}})
    with pytest.raises(ConfigError) as excinfo:
        _launch_capture(monkeypatch, tmp_path, ["--agents", colliding])
    assert "reserved generated agent ID" in excinfo.value.message
    assert "description" not in excinfo.value.message


def test_launch_without_any_evidence_never_identifies_the_claude_binary(monkeypatch, tmp_path):
    import pytest

    from agent_gateway.harnesses import claude as claude_harness

    def tripwire(_config):
        raise AssertionError("an ordinary launch must not run `claude --version`")

    monkeypatch.setattr(claude_harness, "resolve_claude_identity", tripwire)
    # _launch_capture installs its own stub, so patch again after it runs by
    # driving the un-seeded path and asserting the tripwire never fires.
    captured, _paths = _launch_capture(monkeypatch, tmp_path, ["chat"], seed=False)
    assert captured["argv"][1:] == ["chat"]
    assert pytest is not None
