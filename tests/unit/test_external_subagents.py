"""Unit tests for generated external-model agent definitions and ``--agents`` merging.

Claude Code's built-in Agent tool rejects any non-Claude value in its explicit
``model`` argument, so an external model can only reach a native subagent through
a custom-agent *definition*. These tests lock the two halves of that contract:
the deterministic, collision-resistant agent identity, and the exact read-only
definition shape AGW is allowed to generate.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from agent_gateway.errors import ConfigError
from agent_gateway.external_subagents import (
    DEFINITION_CONTRACT_VERSION,
    EXTERNAL_AGENT_MAX_TURNS,
    EXTERNAL_AGENT_TOOLS,
    _sanitized_prefix,
    build_generated_agent,
    build_generated_agents,
    generated_agent_type,
    merge_agents_args,
)
from agent_gateway.model_registry import ModelEntry, ModelMode
from agent_gateway.providers import PROVIDER_PREFIX, Provider


def _entry(name: str, *, provider: Provider = Provider.chatgpt) -> ModelEntry:
    return ModelEntry(
        name=name,
        provider=provider,
        upstream_model=f"{PROVIDER_PREFIX[provider]}{name}",
        mode=ModelMode.responses,
        enabled=True,
    )


def _expected_digest(name: str, provider: str) -> str:
    """Recompute the documented digest independently of the implementation."""
    blob = json.dumps(
        {"contract": 1, "model": name, "provider": provider},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


@pytest.fixture
def sol_entry() -> ModelEntry:
    return _entry("gpt-5.6-sol")


@pytest.fixture
def sol_agent(sol_entry: ModelEntry):
    return build_generated_agent(sol_entry)


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
def test_generated_agent_type_matches_contract_example(sol_entry):
    assert generated_agent_type(sol_entry) == "agw-gpt-5-6-sol-66127d2195a1"


def test_generated_agent_type_digest_is_the_documented_canonical_json(sol_entry):
    assert DEFINITION_CONTRACT_VERSION == 1
    assert generated_agent_type(sol_entry) == (
        "agw-gpt-5-6-sol-" + _expected_digest("gpt-5.6-sol", "chatgpt")
    )


def test_names_sharing_a_sanitized_prefix_keep_distinct_ids():
    # Punctuation replacement alone collapses these two distinct public names.
    dashed = _entry("gpt-5.6-sol")
    dotted = _entry("gpt.5.6.sol")
    assert _sanitized_prefix(dashed.name) == _sanitized_prefix(dotted.name) == "gpt-5-6-sol"

    dashed_type = generated_agent_type(dashed)
    dotted_type = generated_agent_type(dotted)
    assert dashed_type != dotted_type
    assert dashed_type.rsplit("-", 1)[1] != dotted_type.rsplit("-", 1)[1]


def test_same_name_on_a_different_provider_is_a_different_id():
    chatgpt = _entry("shared-model", provider=Provider.chatgpt)
    copilot = _entry("shared-model", provider=Provider.copilot)
    assert generated_agent_type(chatgpt) != generated_agent_type(copilot)
    assert generated_agent_type(copilot) == (
        "agw-shared-model-" + _expected_digest("shared-model", "copilot")
    )


def test_sanitized_prefix_is_lowercased_run_collapsed_and_trimmed():
    assert _sanitized_prefix("GPT-5.6::Sol") == "gpt-5-6-sol"
    assert _sanitized_prefix("--gpt--sol--") == "gpt-sol"
    assert _sanitized_prefix("gpt_5.6_sol") == "gpt-5-6-sol"


def test_sanitized_prefix_truncates_to_forty_characters_without_a_dangling_hyphen():
    prefix = _sanitized_prefix("a" * 40 + ".tail")
    assert prefix == "a" * 40
    # Truncating mid-separator must not leave the id ending in '-'.
    assert _sanitized_prefix("b" * 39 + ".tail") == "b" * 39
    assert len(_sanitized_prefix("c" * 80)) == 40


def test_sanitized_prefix_falls_back_when_nothing_survives():
    # Unreachable through ModelEntry (its name validator requires a leading
    # alphanumeric), so the defensive branch is covered directly.
    assert _sanitized_prefix("...") == "model"


# --------------------------------------------------------------------------- #
# Definition shape
# --------------------------------------------------------------------------- #
def test_generated_definition_is_exactly_read_only(sol_entry):
    agent = build_generated_agent(sol_entry)
    assert agent.model == "gpt-5.6-sol"
    assert agent.provider.value == "chatgpt"
    assert set(agent.definition) == {"description", "prompt", "model", "tools", "maxTurns"}
    assert agent.definition["model"] == "gpt-5.6-sol"
    assert agent.definition["tools"] == list(EXTERNAL_AGENT_TOOLS) == ["Read", "Grep", "Glob"]
    assert agent.definition["maxTurns"] == 20
    assert "ChatGPT" in str(agent.definition["description"])
    assert "read-only" in str(agent.definition["description"])
    assert "sent to ChatGPT" in str(agent.definition["description"])
    assert "do not use merely" in str(agent.definition["description"])


def test_generated_definition_omits_every_escalating_field(sol_entry):
    definition = build_generated_agent(sol_entry).definition
    for forbidden in ("permissionMode", "disallowedTools", "hooks", "mcpServers", "memory"):
        assert forbidden not in definition
    tools = definition["tools"]
    assert isinstance(tools, list)
    for escalation in ("Agent", "Workflow", "Bash", "Edit", "Write", "Skill", "WebFetch"):
        assert escalation not in tools


def test_generated_definition_names_the_copilot_provider_label():
    definition = build_generated_agent(_entry("some-model", provider=Provider.copilot)).definition
    assert "GitHub Copilot" in str(definition["description"])
    assert "sent to GitHub Copilot" in str(definition["description"])


def test_generated_prompt_is_the_pinned_analysis_only_text(sol_entry):
    assert build_generated_agent(sol_entry).definition["prompt"] == (
        "Complete only the delegated analysis task. Use only the provided read-only "
        "repository tools. Do not attempt to modify files or invoke external services. "
        "State limitations clearly and return a concise result to the parent."
    )


def test_agent_type_and_definition_agree(sol_entry):
    agent = build_generated_agent(sol_entry)
    assert agent.agent_type == generated_agent_type(sol_entry)
    assert EXTERNAL_AGENT_MAX_TURNS == 20


# --------------------------------------------------------------------------- #
# Deterministic ordering
# --------------------------------------------------------------------------- #
def test_build_generated_agents_is_sorted_and_deterministic():
    entries = [_entry("gpt-5.6-terra"), _entry("gpt-5.6-luna"), _entry("gpt-5.6-sol")]
    agents = build_generated_agents(entries)
    assert [agent.model for agent in agents] == [
        "gpt-5.6-luna",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
    ]
    assert build_generated_agents(list(reversed(entries))) == agents


def test_build_generated_agents_accepts_an_empty_registry():
    assert build_generated_agents([]) == ()


# --------------------------------------------------------------------------- #
# ``--agents`` merge
# --------------------------------------------------------------------------- #
USER_JSON = '{"mine":{"description":"x"}}'
CANARY = "sk-canary-do-not-leak"


@pytest.mark.parametrize(
    ("args", "expected_tail"),
    [
        ([], []),
        (["--agents", USER_JSON], []),
        ([f"--agents={USER_JSON}"], []),
        (["--", "--agents", "not-json"], ["--", "--agents", "not-json"]),
    ],
)
def test_merge_agents_args_preserves_position_and_terminator(args, expected_tail, sol_agent):
    merged = merge_agents_args(args, [sol_agent])
    option_index = merged.index("--agents")
    parsed = json.loads(merged[option_index + 1])
    assert parsed[sol_agent.agent_type] == sol_agent.definition
    assert merged[option_index + 2 :] == expected_tail


def test_no_generated_definitions_forwards_arguments_byte_for_byte():
    for original in (
        [],
        ["--agents", "not-json"],
        ["--agents"],
        ["--agents", USER_JSON, "--agents", USER_JSON],
        ["-p", "review this", "--", "--agents", "also-not-json"],
    ):
        assert merge_agents_args(original, []) == list(original)


def test_generated_option_is_inserted_before_the_terminator(sol_agent):
    merged = merge_agents_args(["-p", "hi", "--", "trailing"], [sol_agent])
    assert merged[:2] == ["-p", "hi"]
    assert merged[2] == "--agents"
    json.loads(merged[3])
    assert merged[4:] == ["--", "trailing"]


def test_generated_option_is_appended_when_there_is_no_terminator(sol_agent):
    merged = merge_agents_args(["-p", "hi"], [sol_agent])
    assert merged[:2] == ["-p", "hi"]
    assert merged[2] == "--agents"
    json.loads(merged[3])
    assert len(merged) == 4


def test_user_option_is_replaced_in_place_with_two_tokens(sol_agent):
    merged = merge_agents_args(["--verbose", "--agents", USER_JSON, "--print"], [sol_agent])
    assert merged[0] == "--verbose"
    assert merged[1] == "--agents"
    assert merged[3] == "--print"
    assert len(merged) == 4


def test_inline_form_is_replaced_in_place_with_two_tokens(sol_agent):
    merged = merge_agents_args(["--verbose", f"--agents={USER_JSON}", "--print"], [sol_agent])
    assert merged[:2] == ["--verbose", "--agents"]
    assert merged[3] == "--print"
    assert len(merged) == 4


def test_user_values_survive_semantically(sol_agent):
    rich = {
        "mine": {
            "description": "x",
            "nested": {"list": [1, 2.5, True, False, None], "obj": {"k": "v"}},
            "unicode": "héllo ✓",
        }
    }
    merged = merge_agents_args(["--agents", json.dumps(rich)], [sol_agent])
    parsed = json.loads(merged[merged.index("--agents") + 1])
    assert parsed["mine"] == rich["mine"]
    assert parsed[sol_agent.agent_type] == sol_agent.definition


def test_generated_json_is_deterministic_and_compact(sol_agent):
    first = merge_agents_args(["--agents", USER_JSON], [sol_agent])
    second = merge_agents_args(["--agents", USER_JSON], [sol_agent])
    payload = first[first.index("--agents") + 1]
    assert first == second
    assert ", " not in payload and '": ' not in payload


def test_options_after_the_terminator_are_never_parsed(sol_agent):
    merged = merge_agents_args(["--", "--agents", "{broken", "--agents=also-broken"], [sol_agent])
    assert merged[2:] == ["--", "--agents", "{broken", "--agents=also-broken"]


def test_prefix_lookalike_options_are_not_matched(sol_agent):
    merged = merge_agents_args(["--agents-file", "/tmp/a.json"], [sol_agent])
    assert merged[:2] == ["--agents-file", "/tmp/a.json"]
    assert merged[2] == "--agents"
    json.loads(merged[3])


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--agents"], "--agents requires a JSON object value before '--'."),
        (["--agents", "--"], "--agents requires a JSON object value before '--'."),
        (
            ["--agents", "{broken"],
            "--agents must contain valid JSON; the value was not included in this diagnostic.",
        ),
        (["--agents", '"scalar"'], "--agents JSON must have an object root."),
        (["--agents", "[1,2]"], "--agents JSON must have an object root."),
        (["--agents", "null"], "--agents JSON must have an object root."),
        (
            ["--agents", USER_JSON, "--agents", USER_JSON],
            "--agents may be specified at most once before '--'.",
        ),
        (
            ["--agents", USER_JSON, f"--agents={USER_JSON}"],
            "--agents may be specified at most once before '--'.",
        ),
    ],
)
def test_invalid_user_agents_fails_with_the_exact_message(args, message, sol_agent):
    with pytest.raises(ConfigError) as excinfo:
        merge_agents_args(args, [sol_agent])
    assert excinfo.value.message == message


def test_reserved_generated_id_collision_is_refused(sol_agent):
    colliding = json.dumps({sol_agent.agent_type: {"description": "mine"}})
    with pytest.raises(ConfigError) as excinfo:
        merge_agents_args(["--agents", colliding], [sol_agent])
    assert excinfo.value.message == (
        f"--agents defines reserved generated agent ID '{sol_agent.agent_type}'."
    )


def test_diagnostics_never_echo_the_supplied_json(sol_agent):
    secret = json.dumps({"mine": {"description": CANARY, "prompt": CANARY}})
    for args in (["--agents", secret + "{trailing"], ["--agents", f'"{CANARY}"']):
        with pytest.raises(ConfigError) as excinfo:
            merge_agents_args(args, [sol_agent])
        rendered = f"{excinfo.value.message} {excinfo.value.hint or ''}"
        assert CANARY not in rendered
        assert "description" not in rendered


# --------------------------------------------------------------------------- #
# Launch-time eligibility
# --------------------------------------------------------------------------- #
FINGERPRINT = "a" * 64


@pytest.fixture
def eligibility_env(tmp_path):
    from agent_gateway.claude_compat import ClaudeExecutableIdentity
    from agent_gateway.model_registry import load_registry_text
    from agent_gateway.paths import get_paths

    paths = get_paths({"HOME": str(tmp_path)})
    registry = load_registry_text(
        """
        default_model: null
        models:
          - name: gpt-5.6-luna
            provider: chatgpt
            upstream_model: chatgpt/gpt-5.6-luna
            mode: responses
            enabled: true
          - name: gpt-5.6-sol
            provider: chatgpt
            upstream_model: chatgpt/gpt-5.6-sol
            mode: responses
            enabled: true
          - name: gpt-5.6-terra
            provider: chatgpt
            upstream_model: chatgpt/gpt-5.6-terra
            mode: responses
            enabled: false
        """
    )
    claude = ClaudeExecutableIdentity(
        configured_path="/home/me/.local/bin/claude",
        resolved_path="/home/me/.local/share/claude/versions/2.1.232",
        sha256="b" * 64,
        version="2.1.232 (Claude Code)",
    )
    return paths, registry, claude


def _seed(paths, claude, model, *, outcome="passed", fingerprint=FINGERPRINT):
    from agent_gateway.claude_compat import (
        COMPATIBILITY_SCHEMA,
        CompatibilityIdentity,
        CompatibilityRecord,
        utc_now,
        write_compatibility_record,
    )

    write_compatibility_record(
        paths,
        CompatibilityRecord(
            schema=COMPATIBILITY_SCHEMA,
            identity=CompatibilityIdentity(
                model=model,
                provider=Provider.chatgpt,
                claude=claude,
                runtime_fingerprint=fingerprint,
                definition_contract=DEFINITION_CONTRACT_VERSION,
            ),
            verified_at=utc_now(),
            outcome=outcome,
            detail="exact native subagent route verified" if outcome == "passed" else "nope",
        ),
    )


def test_eligibility_covers_every_active_model_only(eligibility_env):
    from agent_gateway.external_subagents import resolve_external_agent_eligibility

    paths, registry, claude = eligibility_env
    rows = resolve_external_agent_eligibility(paths, registry, claude, FINGERPRINT, [])

    assert [row.status.model for row in rows] == ["gpt-5.6-luna", "gpt-5.6-sol"]
    assert all(row.agent is None for row in rows)  # nothing verified yet


def test_only_current_evidence_produces_an_agent(eligibility_env):
    from agent_gateway.claude_compat import CompatibilityState
    from agent_gateway.external_subagents import resolve_external_agent_eligibility

    paths, registry, claude = eligibility_env
    _seed(paths, claude, "gpt-5.6-sol")
    rows = {
        row.status.model: row
        for row in resolve_external_agent_eligibility(paths, registry, claude, FINGERPRINT, [])
    }

    assert rows["gpt-5.6-sol"].status.state is CompatibilityState.current
    assert rows["gpt-5.6-sol"].agent is not None
    assert rows["gpt-5.6-sol"].agent.model == "gpt-5.6-sol"
    assert rows["gpt-5.6-luna"].status.state is CompatibilityState.missing
    assert rows["gpt-5.6-luna"].agent is None


@pytest.mark.parametrize(
    ("kwargs", "state"),
    [
        ({"outcome": "failed"}, "failed"),
        ({"fingerprint": "d" * 64}, "stale"),
    ],
    ids=["failed", "stale"],
)
def test_non_current_evidence_is_never_advertised(eligibility_env, kwargs, state):
    from agent_gateway.external_subagents import resolve_external_agent_eligibility

    paths, registry, claude = eligibility_env
    _seed(paths, claude, "gpt-5.6-sol", **kwargs)
    rows = {
        row.status.model: row
        for row in resolve_external_agent_eligibility(paths, registry, claude, FINGERPRINT, [])
    }
    assert rows["gpt-5.6-sol"].agent is None
    assert rows["gpt-5.6-sol"].status.state.value == state


def test_policy_exclusion_wins_over_current_evidence(eligibility_env, tmp_path):
    from agent_gateway.claude_compat import CompatibilityState
    from agent_gateway.external_subagents import resolve_external_agent_eligibility

    paths, registry, claude = eligibility_env
    _seed(paths, claude, "gpt-5.6-sol")
    settings = [(tmp_path / "user.json", {"availableModels": ["sonnet"]})]
    rows = {
        row.status.model: row
        for row in resolve_external_agent_eligibility(
            paths, registry, claude, FINGERPRINT, settings
        )
    }
    assert rows["gpt-5.6-sol"].agent is None
    assert rows["gpt-5.6-sol"].status.state is CompatibilityState.policy_excluded


def test_current_generated_agents_returns_only_eligible_definitions(eligibility_env):
    from agent_gateway.external_subagents import current_generated_agents

    paths, registry, claude = eligibility_env
    assert current_generated_agents(paths, registry, claude, FINGERPRINT, []) == ()

    _seed(paths, claude, "gpt-5.6-sol")
    _seed(paths, claude, "gpt-5.6-luna")
    agents = current_generated_agents(paths, registry, claude, FINGERPRINT, [])
    assert [agent.model for agent in agents] == ["gpt-5.6-luna", "gpt-5.6-sol"]


def test_inspect_generated_id_collisions_reports_only_reserved_ids(eligibility_env, tmp_path):
    from agent_gateway.external_subagents import (
        current_generated_agents,
        inspect_generated_id_collisions,
    )

    paths, registry, claude = eligibility_env
    _seed(paths, claude, "gpt-5.6-sol")
    agents = current_generated_agents(paths, registry, claude, FINGERPRINT, [])
    reserved = agents[0].agent_type

    user = tmp_path / "user.json"
    project = tmp_path / "project.json"
    settings = [
        (user, {"agents": {reserved: {"description": "mine"}, "other": {}}}),
        (project, {"agents": {"unrelated": {}}}),
        (tmp_path / "none.json", {"availableModels": ["x"]}),
    ]
    assert inspect_generated_id_collisions(settings, agents) == ((user, reserved),)
    # Settings are never mutated by inspection.
    assert settings[0][1] == {"agents": {reserved: {"description": "mine"}, "other": {}}}


def test_inspect_generated_id_collisions_ignores_a_non_object_agents_key(eligibility_env, tmp_path):
    from agent_gateway.external_subagents import inspect_generated_id_collisions

    paths, registry, claude = eligibility_env
    _seed(paths, claude, "gpt-5.6-sol")
    from agent_gateway.external_subagents import current_generated_agents

    agents = current_generated_agents(paths, registry, claude, FINGERPRINT, [])
    settings = [(tmp_path / "s.json", {"agents": ["not", "an", "object"]})]
    assert inspect_generated_id_collisions(settings, agents) == ()
