"""Unit tests for the native-subagent probe hooks and their router registration.

Two command hooks run inside the probe's Claude process. ``PreToolUse`` proves
the parent selected the expected generated type *without* an explicit model
override; ``SubagentStart`` records the child's id and, in the same synchronous
call, registers the one-shot route authorization. That ordering is the whole
point: Claude Code drains ``SubagentStart`` hooks before the child's first model
request, so registering here closes the window that a polling verifier would
leave open.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import httpx
import pytest

from agent_gateway import subagent_probe
from agent_gateway.claude_compat import read_compatibility_record
from agent_gateway.errors import ProxyError
from agent_gateway.paths import ensure_dir
from agent_gateway.probe_events import create_probe_file, read_probe_events
from agent_gateway.router import SUBAGENT_PROBE_PATH
from agent_gateway.subagent_probe import (
    HookContext,
    RouterProbeRegistration,
    hook_context_from_env,
    hook_main,
    pre_tool_use_decision,
    register_probe,
    run_pre_tool_use_hook,
    run_subagent_start_hook,
)

NONCE = "0" * 32
WORKER_TYPE = "agw-gpt-5-6-sol-66127d2195a1"
AGENT_ID = "agent-1"
ROUTER_URL = "http://127.0.0.1:4000"
LOCAL_KEY = "local-key"
DENY_REASON = (
    "AGW native subagent probe permits exactly one Agent call to the expected "
    "generated type without an explicit model override."
)


@pytest.fixture
def probe_dir(tmp_path) -> Path:
    return ensure_dir(tmp_path / "probes", 0o700)


@pytest.fixture
def identity(probe_dir):
    return create_probe_file(probe_dir, NONCE)


@pytest.fixture
def hook_context(probe_dir, identity) -> HookContext:
    return HookContext(
        probe_dir=probe_dir,
        file=identity,
        nonce=NONCE,
        expected_agent_type=WORKER_TYPE,
        expected_model="gpt-5.6-sol",
        expected_provider="chatgpt",
        router_url=ROUTER_URL,
        local_key=LOCAL_KEY,
    )


@pytest.fixture
def hook_env(hook_context) -> dict[str, str]:
    return {
        "AGW_PROBE_DIR": str(hook_context.probe_dir),
        "AGW_PROBE_EVENT_FILE": hook_context.file.path,
        "AGW_PROBE_EVENT_DEVICE": str(hook_context.file.device),
        "AGW_PROBE_EVENT_INODE": str(hook_context.file.inode),
        "AGW_PROBE_EVENT_UID": str(hook_context.file.uid),
        "AGW_PROBE_EVENT_MODE": str(hook_context.file.mode),
        "AGW_PROBE_NONCE": NONCE,
        "AGW_PROBE_EXPECTED_AGENT_TYPE": WORKER_TYPE,
        "AGW_PROBE_EXPECTED_MODEL": "gpt-5.6-sol",
        "AGW_PROBE_EXPECTED_PROVIDER": "chatgpt",
        "AGW_PROBE_ROUTER_URL": ROUTER_URL,
        "AGW_PROBE_LOCAL_KEY": LOCAL_KEY,
    }


def _pre_tool_payload(**overrides):
    override_input = overrides.pop("tool_input", {})
    if isinstance(override_input, dict):
        tool_input: object = {
            "description": "Read the fixture",
            "prompt": "Read it and return the line.",
            "subagent_type": WORKER_TYPE,
            **override_input,
        }
    else:
        tool_input = override_input
    payload = {
        "session_id": "session-1",
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_use_id": "toolu_1",
        "tool_input": tool_input,
    }
    payload.update(overrides)
    return payload


def _start_payload(**overrides):
    payload = {
        "session_id": "session-1",
        "hook_event_name": "SubagentStart",
        "agent_id": AGENT_ID,
        "agent_type": WORKER_TYPE,
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# PreToolUse
# --------------------------------------------------------------------------- #
def test_pre_tool_use_allows_the_expected_call_and_records_only_metadata(
    hook_context, probe_dir, identity
):
    code, response = run_pre_tool_use_hook(hook_context, _pre_tool_payload())

    assert code == 0
    assert response == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        }
    }
    assert read_probe_events(probe_dir, identity) == (
        {
            "schema": 1,
            "event": "pre_tool_use",
            "tool_use_id": "toolu_1",
            "subagent_type": WORKER_TYPE,
            "explicit_model_absent": True,
        },
    )


def test_pre_tool_use_evidence_holds_no_prompt_or_description(hook_context, probe_dir, identity):
    canary = "sk-canary-do-not-leak"
    run_pre_tool_use_hook(
        hook_context,
        _pre_tool_payload(tool_input={"prompt": canary, "description": canary}),
    )
    raw = Path(identity.path).read_text(encoding="utf-8")
    assert canary not in raw
    assert "prompt" not in raw
    assert "description" not in raw


@pytest.mark.parametrize(
    "payload",
    [
        _pre_tool_payload(tool_input={"subagent_type": "general-purpose"}),
        _pre_tool_payload(tool_input={"model": "sonnet"}),
        _pre_tool_payload(tool_input={"model": None}),
        _pre_tool_payload(tool_input={"model": ""}),
    ],
    ids=["wrong-type", "explicit-model", "null-model", "empty-model"],
)
def test_pre_tool_use_denies_a_call_outside_the_probe_contract(hook_context, payload, probe_dir,
                                                              identity):
    code, response = run_pre_tool_use_hook(hook_context, payload)

    assert code == 0
    assert response == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": DENY_REASON,
        }
    }
    assert read_probe_events(probe_dir, identity) == ()


def test_pre_tool_use_denies_a_second_agent_call(hook_context, probe_dir, identity):
    assert run_pre_tool_use_hook(hook_context, _pre_tool_payload())[0] == 0
    _code, response = run_pre_tool_use_hook(hook_context, _pre_tool_payload(tool_use_id="toolu_2"))

    decision = response["hookSpecificOutput"]["permissionDecision"]
    assert decision == "deny"
    assert len(read_probe_events(probe_dir, identity)) == 1


@pytest.mark.parametrize(
    "payload",
    [
        _pre_tool_payload(hook_event_name="PostToolUse"),
        _pre_tool_payload(tool_name="Read"),
        _pre_tool_payload(tool_use_id=7),
        _pre_tool_payload(tool_input="not-a-dict"),
        {"hook_event_name": "PreToolUse"},
    ],
    ids=["wrong-event", "wrong-tool", "bad-id", "bad-input", "missing-fields"],
)
def test_pre_tool_use_rejects_malformed_hook_input(hook_context, payload):
    with pytest.raises(ProxyError):
        run_pre_tool_use_hook(hook_context, payload)


def test_pre_tool_use_decision_builds_both_shapes():
    assert pre_tool_use_decision() == {
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}
    }
    assert pre_tool_use_decision("nope")["hookSpecificOutput"]["permissionDecisionReason"] == "nope"


# --------------------------------------------------------------------------- #
# SubagentStart: append, then register, before returning
# --------------------------------------------------------------------------- #
def test_subagent_start_appends_then_registers_before_return(monkeypatch, hook_context):
    calls: list[str] = []
    monkeypatch.setattr(subagent_probe, "append_probe_event", lambda *a: calls.append("append"))
    monkeypatch.setattr(
        subagent_probe, "register_probe", lambda *a, **k: calls.append("register")
    )
    monkeypatch.setattr(subagent_probe, "read_probe_events", lambda *a: ())

    assert run_subagent_start_hook(hook_context, _start_payload()) == 0
    assert calls == ["append", "register"]


def test_subagent_start_records_only_the_child_identity(monkeypatch, hook_context, probe_dir,
                                                        identity):
    monkeypatch.setattr(subagent_probe, "register_probe", lambda *a, **k: None)

    assert run_subagent_start_hook(hook_context, _start_payload()) == 0
    assert read_probe_events(probe_dir, identity) == (
        {
            "schema": 1,
            "event": "subagent_start",
            "agent_id": AGENT_ID,
            "agent_type": WORKER_TYPE,
        },
    )


def test_subagent_start_registers_the_exact_body(monkeypatch, hook_context, identity):
    sent: list[RouterProbeRegistration] = []
    monkeypatch.setattr(
        subagent_probe,
        "register_probe",
        lambda url, key, registration, **kwargs: sent.append(registration),
    )

    run_subagent_start_hook(hook_context, _start_payload())

    assert sent[0].to_dict() == {
        "schema": 1,
        "nonce": NONCE,
        "agent_id": AGENT_ID,
        "model": "gpt-5.6-sol",
        "provider": "chatgpt",
        "event_file": {
            "path": identity.path,
            "device": identity.device,
            "inode": identity.inode,
            "uid": identity.uid,
            "mode": identity.mode,
        },
    }


@pytest.mark.parametrize(
    "payload",
    [
        _start_payload(agent_type="general-purpose"),
        _start_payload(agent_id=""),
        _start_payload(hook_event_name="SubagentStop"),
        {"hook_event_name": "SubagentStart"},
    ],
    ids=["wrong-type", "empty-id", "wrong-event", "missing-fields"],
)
def test_subagent_start_rejects_an_unexpected_child(monkeypatch, hook_context, payload):
    monkeypatch.setattr(
        subagent_probe,
        "register_probe",
        lambda *a, **k: pytest.fail("must not register an unexpected child"),
    )
    with pytest.raises(ProxyError):
        run_subagent_start_hook(hook_context, payload)


def test_subagent_start_refuses_a_duplicate_start(monkeypatch, hook_context):
    monkeypatch.setattr(subagent_probe, "register_probe", lambda *a, **k: None)
    assert run_subagent_start_hook(hook_context, _start_payload()) == 0
    with pytest.raises(ProxyError):
        run_subagent_start_hook(hook_context, _start_payload())


def test_a_registration_failure_fails_the_hook(monkeypatch, hook_context):
    def boom(*_args, **_kwargs):
        raise ProxyError("could not register the native subagent probe with the local router.")

    monkeypatch.setattr(subagent_probe, "register_probe", boom)
    with pytest.raises(ProxyError):
        run_subagent_start_hook(hook_context, _start_payload())


# --------------------------------------------------------------------------- #
# Router registration client
# --------------------------------------------------------------------------- #
def _registration(identity) -> RouterProbeRegistration:
    return RouterProbeRegistration(
        nonce=NONCE,
        agent_id=AGENT_ID,
        model="gpt-5.6-sol",
        provider="chatgpt",
        file=identity,
    )


def test_register_probe_posts_the_authenticated_control_request(identity):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"ok": True, "expires_in": 60})

    register_probe(
        ROUTER_URL,
        LOCAL_KEY,
        _registration(identity),
        transport=httpx.MockTransport(handler),
    )

    assert seen[0].url == httpx.URL(f"{ROUTER_URL}{SUBAGENT_PROBE_PATH}")
    assert seen[0].headers["x-agw-key"] == LOCAL_KEY
    assert json.loads(seen[0].content) == _registration(identity).to_dict()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"ok": True, "expires_in": 60}),
        httpx.Response(409, json={"error": {"message": "already registered"}}),
        httpx.Response(201, json={"ok": False, "expires_in": 60}),
        httpx.Response(201, json={"ok": True}),
        httpx.Response(201, content=b"not json"),
    ],
    ids=["wrong-status", "conflict", "not-ok", "missing-field", "unparsable"],
)
def test_register_probe_requires_the_exact_success_response(identity, response):
    with pytest.raises(ProxyError) as excinfo:
        register_probe(
            ROUTER_URL,
            LOCAL_KEY,
            _registration(identity),
            transport=httpx.MockTransport(lambda _request: response),
        )
    assert excinfo.value.message == (
        "could not register the native subagent probe with the local router."
    )


def test_register_probe_maps_a_transport_failure(identity):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ProxyError):
        register_probe(
            ROUTER_URL,
            LOCAL_KEY,
            _registration(identity),
            transport=httpx.MockTransport(handler),
        )


def test_register_probe_never_echoes_the_key_or_response(identity):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"internal detail with local-key")

    with pytest.raises(ProxyError) as excinfo:
        register_probe(
            ROUTER_URL,
            LOCAL_KEY,
            _registration(identity),
            transport=httpx.MockTransport(handler),
        )
    rendered = f"{excinfo.value.message} {excinfo.value.hint or ''}"
    assert LOCAL_KEY not in rendered
    assert "internal detail" not in rendered


# --------------------------------------------------------------------------- #
# Environment and CLI wiring
# --------------------------------------------------------------------------- #
def test_hook_context_is_built_from_the_environment(hook_env, hook_context):
    assert hook_context_from_env(hook_env) == hook_context


@pytest.mark.parametrize(
    "missing",
    [
        "AGW_PROBE_DIR",
        "AGW_PROBE_EVENT_FILE",
        "AGW_PROBE_NONCE",
        "AGW_PROBE_EXPECTED_AGENT_TYPE",
        "AGW_PROBE_ROUTER_URL",
        "AGW_PROBE_LOCAL_KEY",
    ],
)
def test_hook_context_requires_every_variable(hook_env, missing):
    del hook_env[missing]
    with pytest.raises(ProxyError):
        hook_context_from_env(hook_env)


def test_hook_context_requires_numeric_identity_fields(hook_env):
    hook_env["AGW_PROBE_EVENT_INODE"] = "not-a-number"
    with pytest.raises(ProxyError):
        hook_context_from_env(hook_env)


def test_hook_main_reads_stdin_and_writes_the_decision(hook_env, monkeypatch):
    monkeypatch.setattr(os, "environ", hook_env)
    stdout = io.StringIO()
    code = hook_main(
        ["hook", "pre-tool-use"],
        stdin=io.StringIO(json.dumps(_pre_tool_payload())),
        stdout=stdout,
    )
    assert code == 0
    assert json.loads(stdout.getvalue())["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_hook_main_reports_a_failure_without_echoing_anything(hook_env, monkeypatch, capsys):
    monkeypatch.setattr(os, "environ", hook_env)
    canary = "sk-canary-do-not-leak"
    code = hook_main(
        ["hook", "pre-tool-use"],
        stdin=io.StringIO(json.dumps({"hook_event_name": "PreToolUse", "secret": canary})),
        stdout=io.StringIO(),
    )
    assert code != 0
    captured = capsys.readouterr()
    assert captured.err.strip() == "subagent probe hook failed"
    assert canary not in captured.err


def test_hook_main_rejects_an_unknown_subcommand(hook_env, monkeypatch):
    monkeypatch.setattr(os, "environ", hook_env)
    assert hook_main(["hook", "unknown"], stdin=io.StringIO("{}"), stdout=io.StringIO()) != 0


def test_hook_main_bounds_the_stdin_read(hook_env, monkeypatch):
    monkeypatch.setattr(os, "environ", hook_env)
    oversized = json.dumps({"hook_event_name": "PreToolUse", "pad": "x" * 100_000})
    assert hook_main(
        ["hook", "pre-tool-use"], stdin=io.StringIO(oversized), stdout=io.StringIO()
    ) != 0


# --------------------------------------------------------------------------- #
# Verifier definitions, settings, and argv
# --------------------------------------------------------------------------- #
VERIFIER_TYPE = "agw-native-subagent-verifier"


@pytest.fixture
def worker():
    from agent_gateway.external_subagents import build_generated_agent
    from agent_gateway.model_registry import ModelEntry, ModelMode
    from agent_gateway.providers import Provider

    return build_generated_agent(
        ModelEntry(
            name="gpt-5.6-sol",
            provider=Provider.chatgpt,
            upstream_model="chatgpt/gpt-5.6-sol",
            mode=ModelMode.responses,
            enabled=True,
        )
    )


def test_verifier_agents_pair_a_native_parent_with_the_external_worker(worker):
    agents = subagent_probe.build_verifier_agents(worker)

    assert set(agents) == {VERIFIER_TYPE, worker.agent_type}
    assert agents[worker.agent_type] == worker.definition
    assert agents[VERIFIER_TYPE] == {
        "description": "Run one controlled AGW native-subagent compatibility check.",
        "prompt": (
            "Call the expected generated AGW agent exactly once. Omit the Agent model "
            "override. Ask it to read the supplied one-line fixture and return its "
            "content. Do not perform any other task."
        ),
        "model": "sonnet",
        "tools": ["Agent"],
        "maxTurns": 4,
    }


def test_the_parent_cannot_read_and_the_worker_cannot_spawn(worker):
    agents = subagent_probe.build_verifier_agents(worker)
    assert agents[VERIFIER_TYPE]["tools"] == ["Agent"]
    assert agents[worker.agent_type]["tools"] == ["Read", "Grep", "Glob"]


def test_probe_settings_hold_only_the_two_session_hooks():
    settings = subagent_probe.build_probe_settings("/usr/bin/python3", WORKER_TYPE)
    assert set(settings) == {"hooks"}
    hooks = settings["hooks"]
    assert set(hooks) == {"PreToolUse", "SubagentStart"}
    assert hooks["PreToolUse"][0]["matcher"] == "Agent"
    assert hooks["SubagentStart"][0]["matcher"] == WORKER_TYPE
    for event, subcommand in (("PreToolUse", "pre-tool-use"), ("SubagentStart", "subagent-start")):
        hook = hooks[event][0]["hooks"][0]
        assert hook == {
            "type": "command",
            "command": "/usr/bin/python3",
            "args": ["-I", "-m", "agent_gateway.subagent_probe", "hook", subcommand],
            "timeout": 30,
        }


def test_probe_settings_carry_no_secret(hook_context):
    rendered = json.dumps(subagent_probe.build_probe_settings("/usr/bin/python3", WORKER_TYPE))
    assert LOCAL_KEY not in rendered
    assert NONCE not in rendered
    assert hook_context.file.path not in rendered


def test_probe_env_adds_only_the_hook_variables_and_managed_routing(hook_context, tmp_path):
    context = subagent_probe.ProbeContext(
        hook=hook_context,
        run_dir=tmp_path / "run",
        fixture_name="fixture.txt",
    )
    base = {
        "PATH": "/usr/bin",
        "HOME": "/home/me",
        "ANTHROPIC_AUTH_TOKEN": "leak",
        "ANTHROPIC_API_KEY": "leak",
        "CHATGPT_TOKEN_DIR": "/creds",
        "CHATGPT_AUTH_FILE": "/creds/auth.json",
        "GITHUB_COPILOT_TOKEN_DIR": "/creds/copilot",
        "LITELLM_MASTER_KEY": "master",
        "CLAUDE_CODE_SUBAGENT_MODEL": "haiku",
    }
    env = subagent_probe.build_probe_env(base, context, base_url="http://127.0.0.1:4000")

    for dropped in (
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "CHATGPT_TOKEN_DIR",
        "CHATGPT_AUTH_FILE",
        "GITHUB_COPILOT_TOKEN_DIR",
        "LITELLM_MASTER_KEY",
        "CLAUDE_CODE_SUBAGENT_MODEL",
    ):
        assert dropped not in env

    assert env["PATH"] == "/usr/bin"
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4000"
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == f"X-AGW-Key: {LOCAL_KEY}"
    assert env["AGW_PROBE_NONCE"] == NONCE
    assert env["AGW_PROBE_LOCAL_KEY"] == LOCAL_KEY
    assert env["AGW_PROBE_EXPECTED_AGENT_TYPE"] == WORKER_TYPE

    added = set(env) - set(base) - {"ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS"}
    assert added == {
        "AGW_PROBE_DIR",
        "AGW_PROBE_EVENT_FILE",
        "AGW_PROBE_EVENT_DEVICE",
        "AGW_PROBE_EVENT_INODE",
        "AGW_PROBE_EVENT_UID",
        "AGW_PROBE_EVENT_MODE",
        "AGW_PROBE_NONCE",
        "AGW_PROBE_EXPECTED_AGENT_TYPE",
        "AGW_PROBE_EXPECTED_MODEL",
        "AGW_PROBE_EXPECTED_PROVIDER",
        "AGW_PROBE_ROUTER_URL",
        "AGW_PROBE_LOCAL_KEY",
    }


def test_probe_env_round_trips_into_a_hook_context(hook_context, tmp_path):
    context = subagent_probe.ProbeContext(
        hook=hook_context, run_dir=tmp_path / "run", fixture_name="fixture.txt"
    )
    env = subagent_probe.build_probe_env({}, context, base_url="http://127.0.0.1:4000")
    assert hook_context_from_env(env) == hook_context


def test_probe_argv_is_bounded_and_isolated(worker, tmp_path):
    argv = subagent_probe.build_probe_argv(
        tmp_path / "claude", worker, '{"hooks":{}}', '{"a":1}', "do the thing"
    )

    assert argv[0] == str(tmp_path / "claude")
    assert argv[-1] == "do the thing"

    def value(flag: str) -> str:
        return argv[argv.index(flag) + 1]

    assert "--print" in argv
    assert value("--output-format") == "stream-json"
    # Claude refuses `--print --output-format=stream-json` without it.
    assert "--verbose" in argv
    assert "--include-hook-events" in argv
    assert "--no-session-persistence" in argv
    assert "--disable-slash-commands" in argv
    assert "--no-chrome" in argv
    assert "--strict-mcp-config" in argv
    # `{}` is rejected: Claude validates the payload and requires an mcpServers
    # record, so the empty config has to spell it out.
    assert value("--mcp-config") == '{"mcpServers":{}}'
    # The session set is the union both definitions need; per-agent restriction
    # comes from the definitions themselves.
    assert value("--tools") == "Agent,Read,Grep,Glob"
    assert value("--allowedTools") == "Agent,Read,Grep,Glob"
    assert value("--disallowedTools") == "Bash,Edit,Write,Skill,WebFetch,WebSearch,Workflow"
    assert value("--setting-sources") == "local"
    assert value("--settings") == '{"hooks":{}}'
    assert value("--agents") == '{"a":1}'
    assert value("--agent") == VERIFIER_TYPE
    assert value("--max-budget-usd") == "1.00"
    # Flags the installed client does not support at top level must stay out.
    assert "--max-turns" not in argv
    assert "--safe-mode" not in argv


def test_probe_argv_carries_no_secret(worker, tmp_path):
    argv = subagent_probe.build_probe_argv(
        tmp_path / "claude", worker, "{}", "{}", "prompt"
    )
    assert not any(LOCAL_KEY in token or NONCE in token for token in argv)


def test_probe_prompt_names_the_worker_type_and_bare_fixture(worker):
    prompt = subagent_probe.build_probe_prompt(worker.agent_type, "fixture-abc.txt")
    assert prompt == (
        f"Use Agent exactly once with subagent_type {worker.agent_type}, omit model, and ask "
        "it to read fixture-abc.txt and return the exact one-line content. Then return only "
        "that child result."
    )
    assert "/" not in prompt.split("read ")[1].split(" ")[0]

# --------------------------------------------------------------------------- #
# Orchestration, evidence join, and cleanup
# --------------------------------------------------------------------------- #
class _FakeProc:
    """A stand-in for the probe's Claude process."""

    def __init__(self, stdout: str = "", *, returncode: int = 0, timeout: bool = False) -> None:
        self.pid = 2**31 - 1  # never resolves to a live process
        self._stdout = stdout
        self.returncode = returncode
        self._timeout = timeout

    def communicate(self, timeout=None):
        import subprocess as _subprocess

        if self._timeout:
            raise _subprocess.TimeoutExpired(cmd="claude", timeout=timeout or 0)
        return self._stdout, ""


def _result_stream(text: str) -> str:
    return (
        json.dumps({"type": "assistant", "message": {"content": []}})
        + "\n"
        + json.dumps({"type": "result", "subtype": "success", "result": text})
        + "\n"
    )


def _evidence(env, canary, **overrides):
    """The three events a fully successful probe produces."""
    agent_id = overrides.get("agent_id", "agent-1")
    return [
        {
            "schema": 1,
            "event": "pre_tool_use",
            "tool_use_id": "toolu_1",
            "subagent_type": overrides.get("subagent_type", WORKER_TYPE),
            "explicit_model_absent": overrides.get("explicit_model_absent", True),
        },
        {
            "schema": 1,
            "event": "subagent_start",
            "agent_id": agent_id,
            "agent_type": overrides.get("start_type", WORKER_TYPE),
        },
        {
            "schema": 1,
            "event": "route",
            "nonce": overrides.get("nonce", env["AGW_PROBE_NONCE"]),
            "agent_id": overrides.get("route_agent_id", agent_id),
            "requested_model": overrides.get("requested_model", "gpt-5.6-sol"),
            "resolved_model": overrides.get("resolved_model", "gpt-5.6-sol"),
            "provider": overrides.get("provider", "chatgpt"),
            "request_kind": overrides.get("request_kind", "messages"),
            "timestamp": "2026-08-14T12:00:00Z",
        },
    ]


@pytest.fixture
def verify_env(tmp_path, monkeypatch):
    """A complete offline verification environment; no real Claude is started."""
    from agent_gateway import proxy as proxy_mod
    from agent_gateway.claude_compat import ClaudeExecutableIdentity
    from agent_gateway.config import GatewayConfig
    from agent_gateway.model_registry import load_registry_text
    from agent_gateway.paths import get_paths
    from agent_gateway.process import ProcessIdentity

    paths = get_paths({"HOME": str(tmp_path / "home")})
    ensure_dir(paths.probe_dir)
    registry = load_registry_text(
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
    claude = tmp_path / "claude"
    claude.write_text("#!/bin/sh\n")
    claude.chmod(0o755)
    config = GatewayConfig(port=4000, native_claude_path=str(claude))

    monkeypatch.setattr(
        subagent_probe,
        "resolve_claude_identity",
        lambda _config: ClaudeExecutableIdentity(
            configured_path=str(claude),
            resolved_path=str(claude),
            sha256="b" * 64,
            version="2.1.232 (Claude Code)",
        ),
    )
    monkeypatch.setattr(subagent_probe, "terminate_process_group", lambda *a, **k: True)

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
    return paths, config, registry, state


def _verify(verify_env, monkeypatch, *, evidence=_evidence, stdout=None, returncode=0,
            timeout=False, capture=None):
    paths, config, registry, state = verify_env
    entry = registry.get_active("gpt-5.6-sol")

    def fake_popen(argv, **kwargs):
        env = kwargs["env"]
        run_dir = Path(kwargs["cwd"])
        fixture = next(run_dir.iterdir())
        canary = fixture.read_text(encoding="utf-8").strip()
        if capture is not None:
            capture.update({"argv": argv, "kwargs": kwargs, "canary": canary,
                            "run_dir": run_dir})
        identity = subagent_probe.probe_file_identity_from_env(env)
        for event in evidence(env, canary):
            subagent_probe.append_probe_event(paths.probe_dir, identity, event)
        text = _result_stream(canary) if stdout is None else stdout
        return _FakeProc(text, returncode=returncode, timeout=timeout)

    return subagent_probe.verify_native_subagent(
        paths, config, registry, state, LOCAL_KEY, entry, popen=fake_popen
    )


def test_a_complete_evidence_chain_passes_and_is_recorded(verify_env, monkeypatch):
    from agent_gateway.claude_compat import CompatibilityState, compatibility_status
    from agent_gateway.providers import Provider

    report = _verify(verify_env, monkeypatch)

    assert report.ok is True
    assert report.model == "gpt-5.6-sol"
    assert report.provider == "chatgpt"
    assert report.detail == "exact native subagent route verified"

    paths, _config, _registry, _state = verify_env
    record = read_compatibility_record(paths, "gpt-5.6-sol", Provider.chatgpt)
    assert record is not None
    assert record.outcome == "passed"
    assert record.identity.claude.version == "2.1.232 (Claude Code)"
    assert record.identity.runtime_fingerprint == "a" * 64
    status = compatibility_status(paths, record.identity, settings=[])
    assert status.state is CompatibilityState.current


def test_the_probe_runs_in_its_own_working_directory(verify_env, monkeypatch):
    capture: dict = {}
    _verify(verify_env, monkeypatch, capture=capture)

    paths, _config, _registry, _state = verify_env
    run_dir = capture["run_dir"]
    assert run_dir.parent == paths.probe_dir
    assert run_dir.name.startswith("run-")
    assert not (run_dir / ".claude").exists()
    kwargs = capture["kwargs"]
    assert kwargs["start_new_session"] is True
    assert kwargs["text"] is True
    # The bare fixture name, not a path, is what reaches the prompt.
    assert capture["argv"][-1].count("/") == 0


def test_every_probe_artifact_is_removed(verify_env, monkeypatch):
    _verify(verify_env, monkeypatch)
    paths, _config, _registry, _state = verify_env
    assert list(paths.probe_dir.iterdir()) == []


def test_artifacts_are_removed_even_when_the_probe_fails(verify_env, monkeypatch):
    _verify(verify_env, monkeypatch, returncode=7)
    paths, _config, _registry, _state = verify_env
    assert list(paths.probe_dir.iterdir()) == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"subagent_type": "general-purpose"},
        {"explicit_model_absent": False},
        {"start_type": "general-purpose"},
        {"agent_id": ""},
        {"route_agent_id": "agent-2"},
        {"nonce": "f" * 32},
        {"requested_model": "gpt-5.6-luna"},
        {"resolved_model": "gpt-5.6-luna"},
        {"provider": "copilot"},
        {"request_kind": "count_tokens"},
    ],
    ids=[
        "wrong-type",
        "explicit-model",
        "wrong-start-type",
        "empty-agent-id",
        "agent-id-mismatch",
        "nonce-mismatch",
        "requested-model",
        "resolved-model",
        "provider",
        "count-tokens-only",
    ],
)
def test_any_evidence_mismatch_fails_closed(verify_env, monkeypatch, overrides):
    from agent_gateway.providers import Provider

    report = _verify(
        verify_env,
        monkeypatch,
        evidence=lambda env, canary: _evidence(env, canary, **overrides),
    )
    assert report.ok is False

    paths, _config, _registry, _state = verify_env
    record = read_compatibility_record(paths, "gpt-5.6-sol", Provider.chatgpt)
    assert record is not None
    assert record.outcome == "failed"


@pytest.mark.parametrize("index", [0, 1, 2], ids=["no-pre-tool", "no-start", "no-route"])
def test_a_missing_event_fails_closed(verify_env, monkeypatch, index):
    def partial(env, canary):
        events = _evidence(env, canary)
        del events[index]
        return events

    assert _verify(verify_env, monkeypatch, evidence=partial).ok is False


def test_duplicate_events_fail_closed(verify_env, monkeypatch):
    def duplicated(env, canary):
        events = _evidence(env, canary)
        return [*events, events[2]]

    assert _verify(verify_env, monkeypatch, evidence=duplicated).ok is False


def test_out_of_order_events_fail_closed(verify_env, monkeypatch):
    def reordered(env, canary):
        events = _evidence(env, canary)
        return [events[1], events[0], events[2]]

    assert _verify(verify_env, monkeypatch, evidence=reordered).ok is False


def test_no_evidence_at_all_fails_closed(verify_env, monkeypatch):
    assert _verify(verify_env, monkeypatch, evidence=lambda env, canary: []).ok is False


def test_a_nonzero_exit_with_a_successful_result_still_passes(verify_env, monkeypatch):
    # Claude Code exits non-zero merely for printing `[claude-code:unrecognized_model]`
    # about an external name it does not know, even after routing it, running it, and
    # completing successfully. The final result object is the authoritative outcome.
    assert _verify(verify_env, monkeypatch, returncode=1).ok is True


def test_a_nonzero_exit_without_a_result_fails_closed(verify_env, monkeypatch):
    report = _verify(
        verify_env,
        monkeypatch,
        returncode=7,
        stdout=json.dumps({"type": "system", "subtype": "init"}) + "\n",
    )
    assert report.ok is False
    assert report.detail == "native Claude subagent probe exited non-zero"


def test_an_errored_result_fails_even_with_exit_zero(verify_env, monkeypatch):
    report = _verify(
        verify_env,
        monkeypatch,
        stdout=json.dumps(
            {"type": "result", "subtype": "error_during_execution", "is_error": True}
        )
        + "\n",
    )
    assert report.ok is False


def test_a_timeout_fails_with_the_fixed_detail(verify_env, monkeypatch):
    report = _verify(verify_env, monkeypatch, timeout=True)
    assert report.ok is False
    assert report.detail == "native Claude subagent probe timed out"


def test_a_backgrounded_subagent_result_still_counts(verify_env, monkeypatch):
    # Claude may spawn the child in the background, answer immediately, and deliver
    # the real content later as a task notification — a second result object.
    def two_results(canary: str) -> str:
        return (
            json.dumps(
                {"type": "result", "subtype": "success", "result": "It's running now."}
            )
            + "\n"
            + json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "origin": {"kind": "task-notification"},
                    "result": canary,
                }
            )
            + "\n"
        )

    paths, config, registry, state = verify_env
    entry = registry.get_active("gpt-5.6-sol")

    def fake_popen(argv, **kwargs):
        env = kwargs["env"]
        run_dir = Path(kwargs["cwd"])
        canary = next(run_dir.iterdir()).read_text(encoding="utf-8").strip()
        identity = subagent_probe.probe_file_identity_from_env(env)
        for event in _evidence(env, canary):
            subagent_probe.append_probe_event(paths.probe_dir, identity, event)
        return _FakeProc(two_results(canary), returncode=1)

    report = subagent_probe.verify_native_subagent(
        paths, config, registry, state, LOCAL_KEY, entry, popen=fake_popen
    )
    assert report.ok is True


def test_a_plausible_answer_without_the_read_round_trip_fails(verify_env, monkeypatch):
    # Full route evidence, but the child never returned the fixture content.
    report = _verify(verify_env, monkeypatch, stdout=_result_stream("looks fine to me"))
    assert report.ok is False


def test_a_missing_final_result_fails(verify_env, monkeypatch):
    report = _verify(
        verify_env,
        monkeypatch,
        stdout=json.dumps({"type": "assistant", "message": {"content": []}}) + "\n",
    )
    assert report.ok is False


@pytest.mark.parametrize(
    "warning",
    [
        "model availableModels restriction",
        "that model is not allowed here",
        "model restriction applied",
        "substituted the parent model",
        "falling back to sonnet",
    ],
)
def test_a_substitution_warning_fails_even_with_full_evidence(verify_env, monkeypatch, warning):
    paths, _config, _registry, _state = verify_env
    captured: dict = {}

    def with_warning(env, canary):
        captured["canary"] = canary
        return _evidence(env, canary)

    report = _verify(
        verify_env,
        monkeypatch,
        evidence=with_warning,
        stdout=json.dumps({"type": "system", "text": warning})
        + "\n"
        + json.dumps({"type": "result", "subtype": "success", "result": "PLACEHOLDER"})
        + "\n",
    )
    assert report.ok is False


def test_a_failed_record_never_stores_output_or_paths(verify_env, monkeypatch):

    canary_leak = "sk-canary-do-not-leak"
    _verify(
        verify_env,
        monkeypatch,
        evidence=lambda env, canary: [],
        stdout=_result_stream(canary_leak),
    )
    paths, _config, _registry, _state = verify_env
    text = paths.compatibility_file("gpt-5.6-sol", "chatgpt").read_text(encoding="utf-8")
    assert canary_leak not in text
    assert "probes" not in text


def test_previous_success_is_invalidated_before_the_probe_runs(verify_env, monkeypatch):
    from agent_gateway.providers import Provider

    paths, _config, _registry, _state = verify_env
    assert _verify(verify_env, monkeypatch).ok is True

    def exploding_popen(argv, **kwargs):
        raise KeyboardInterrupt

    _paths, config, registry, state = verify_env
    entry = registry.get_active("gpt-5.6-sol")
    with pytest.raises(KeyboardInterrupt):
        subagent_probe.verify_native_subagent(
            paths, config, registry, state, LOCAL_KEY, entry, popen=exploding_popen
        )

    record = read_compatibility_record(paths, "gpt-5.6-sol", Provider.chatgpt)
    assert record is not None
    assert record.outcome == "failed"
    assert record.detail == "verification in progress; previous success invalidated"
    assert list(paths.probe_dir.iterdir()) == []
