"""Command hooks that witness one controlled native-subagent compatibility probe.

Claude Code runs these as ordinary command hooks inside the probe's own Claude
process. Between them they answer the only question the probe exists to settle:
did a *native custom subagent* whose definition names an external model actually
reach AGW under that exact model, or did Claude quietly inherit its parent's
Claude model instead?

``PreToolUse`` proves the parent selected the expected generated type and did not
pass an explicit model override — the Agent tool's ``model`` argument only
accepts Claude names, so its presence would invalidate the whole experiment.

``SubagentStart`` does two things in one synchronous call: it records the child's
allocated id, then registers a one-shot route authorization with the running
router. The order matters and the synchrony matters more. Claude Code allocates
the child id and drains every matching ``SubagentStart`` hook *before* starting
the child's model loop, so registering here is guaranteed to precede the child's
first request. A verifier that instead watched for the start event and
registered afterwards would race that first request and could miss it.

The hooks never see a prompt, a response, or a tool result. They record ids and
booleans, and their failures say nothing but that they failed.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal, TextIO

import httpx

from agent_gateway.claude_compat import (
    COMPATIBILITY_SCHEMA,
    CompatibilityIdentity,
    CompatibilityRecord,
    invalidate_compatibility,
    resolve_claude_identity,
    utc_now,
    write_compatibility_record,
)
from agent_gateway.config import GatewayConfig
from agent_gateway.errors import ProxyError
from agent_gateway.external_subagents import (
    DEFINITION_CONTRACT_VERSION,
    GeneratedAgent,
    build_generated_agent,
)
from agent_gateway.model_registry import ModelEntry, ModelRegistry
from agent_gateway.paths import Paths, atomic_write_text, ensure_dir
from agent_gateway.probe_events import (
    PROBE_EVENT_SCHEMA,
    ProbeFileIdentity,
    append_probe_event,
    create_probe_file,
    read_probe_events,
    remove_probe_file,
)
from agent_gateway.process import identity_of, terminate_process_group
from agent_gateway.proxy import ProxyState
from agent_gateway.router import SUBAGENT_PROBE_PATH

# Injection point for the probe's child process, so tests never start Claude.
PopenFactory = Callable[..., "subprocess.Popen[str]"]

REGISTRATION_TIMEOUT_SECONDS: Final[float] = 5.0
_MAX_HOOK_INPUT_BYTES: Final[int] = 64 * 1024

VERIFIER_PARENT_TYPE: Final[str] = "agw-native-subagent-verifier"
HOOK_TIMEOUT_SECONDS: Final[int] = 30
PROBE_PROCESS_TIMEOUT_SECONDS: Final[float] = 90.0
PROBE_TERM_TIMEOUT_SECONDS: Final[float] = 5.0
PROBE_KILL_TIMEOUT_SECONDS: Final[float] = 5.0
PROBE_BUDGET_USD: Final[str] = "1.00"
_MAX_PROBE_OUTPUT_BYTES: Final[int] = 256 * 1024

# The session tool set is the *union* the two definitions need. Narrowing it to
# the verifier parent's own contract would remove Read from the built-in set and
# make the worker's required read impossible; per-agent restriction still comes
# from each definition's own `tools` list.
_SESSION_TOOLS: Final[str] = "Agent,Read,Grep,Glob"
_SESSION_DISALLOWED: Final[str] = "Bash,Edit,Write,Skill,WebFetch,WebSearch,Workflow"

_VERIFIER_PROMPT: Final[str] = (
    "Call the expected generated AGW agent exactly once. Omit the Agent model "
    "override. Ask it to read the supplied one-line fixture and return its "
    "content. Do not perform any other task."
)

# Wording Claude Code uses when it refuses or replaces a definition-level model.
# Any of these in the probe output means the run cannot be read as a success.
_SUBSTITUTION_MARKERS: Final[tuple[str, ...]] = (
    "availablemodels",
    "not allowed",
    "model restriction",
    "substitut",
    "falling back",
)

_SCRUBBED_VARS: Final[tuple[str, ...]] = (
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "CHATGPT_TOKEN_DIR",
    "CHATGPT_AUTH_FILE",
    "GITHUB_COPILOT_TOKEN_DIR",
    "LITELLM_MASTER_KEY",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)

_HOOK_FAILED: Final[str] = "subagent probe hook failed"
_REGISTRATION_FAILED: Final[str] = (
    "could not register the native subagent probe with the local router."
)
_DENY_REASON: Final[str] = (
    "AGW native subagent probe permits exactly one Agent call to the expected "
    "generated type without an explicit model override."
)

_REQUIRED_VARS: Final[tuple[str, ...]] = (
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
)


@dataclass(frozen=True)
class HookContext:
    """Everything a hook needs, assembled from the environment only.

    The local key lives here and nowhere else: it is passed to the Claude process
    as an environment variable, never written into settings, argv, or evidence.
    """

    probe_dir: Path
    file: ProbeFileIdentity
    nonce: str
    expected_agent_type: str
    expected_model: str
    expected_provider: str
    router_url: str
    local_key: str


@dataclass(frozen=True)
class RouterProbeRegistration:
    """The exact control-endpoint body, mirrored field for field."""

    nonce: str
    agent_id: str
    model: str
    provider: str
    file: ProbeFileIdentity

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": 1,
            "nonce": self.nonce,
            "agent_id": self.agent_id,
            "model": self.model,
            "provider": self.provider,
            "event_file": {
                "path": self.file.path,
                "device": self.file.device,
                "inode": self.file.inode,
                "uid": self.file.uid,
                "mode": self.file.mode,
            },
        }


def _failed() -> ProxyError:
    return ProxyError(_HOOK_FAILED)


def hook_context_from_env(env: dict[str, str]) -> HookContext:
    """Build the context, refusing to run on a partially configured environment."""
    missing = [name for name in _REQUIRED_VARS if not env.get(name)]
    if missing:
        raise _failed()
    try:
        identity = ProbeFileIdentity(
            path=env["AGW_PROBE_EVENT_FILE"],
            device=int(env["AGW_PROBE_EVENT_DEVICE"]),
            inode=int(env["AGW_PROBE_EVENT_INODE"]),
            uid=int(env["AGW_PROBE_EVENT_UID"]),
            mode=int(env["AGW_PROBE_EVENT_MODE"]),
        )
    except ValueError as exc:
        raise _failed() from exc
    return HookContext(
        probe_dir=Path(env["AGW_PROBE_DIR"]),
        file=identity,
        nonce=env["AGW_PROBE_NONCE"],
        expected_agent_type=env["AGW_PROBE_EXPECTED_AGENT_TYPE"],
        expected_model=env["AGW_PROBE_EXPECTED_MODEL"],
        expected_provider=env["AGW_PROBE_EXPECTED_PROVIDER"],
        router_url=env["AGW_PROBE_ROUTER_URL"],
        local_key=env["AGW_PROBE_LOCAL_KEY"],
    )


def pre_tool_use_decision(reason: str | None = None) -> dict[str, object]:
    """Build the modern hook decision. ``None`` allows; any string denies."""
    output: dict[str, object] = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow" if reason is None else "deny",
    }
    if reason is not None:
        output["permissionDecisionReason"] = reason
    return {"hookSpecificOutput": output}


def pre_tool_use_event(
    payload: dict[str, object],
    expected_type: str,
    existing_events: tuple[dict[str, object], ...],
) -> dict[str, object] | None:
    """Return the sanitized evidence, or ``None`` when the call must be denied.

    Raises when the hook input itself is unusable, which is a probe failure
    rather than a policy decision.
    """
    if payload.get("hook_event_name") != "PreToolUse":
        raise _failed()
    if payload.get("tool_name") != "Agent":
        raise _failed()
    tool_use_id = payload.get("tool_use_id")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        raise _failed()
    if not isinstance(tool_input, dict):
        raise _failed()

    # `model` present at all — even null or empty — means the parent tried the
    # enum-constrained override, so this call cannot prove definition routing.
    if "model" in tool_input:
        return None
    if tool_input.get("subagent_type") != expected_type:
        return None
    if any(event.get("event") == "pre_tool_use" for event in existing_events):
        return None

    return {
        "schema": PROBE_EVENT_SCHEMA,
        "event": "pre_tool_use",
        "tool_use_id": tool_use_id,
        "subagent_type": expected_type,
        "explicit_model_absent": True,
    }


def subagent_start_event(payload: dict[str, object], expected_type: str) -> dict[str, object]:
    """Return the sanitized start evidence for the expected child."""
    if payload.get("hook_event_name") != "SubagentStart":
        raise _failed()
    if payload.get("agent_type") != expected_type:
        raise _failed()
    agent_id = payload.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id:
        raise _failed()
    return {
        "schema": PROBE_EVENT_SCHEMA,
        "event": "subagent_start",
        "agent_id": agent_id,
        "agent_type": expected_type,
    }


def register_probe(
    router_url: str,
    local_key: str,
    registration: RouterProbeRegistration,
    *,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Authorize exactly one route recording, or fail the probe.

    Every failure collapses to one message: the response body can echo local
    detail, and the key must never appear in output.
    """
    try:
        with httpx.Client(transport=transport, timeout=REGISTRATION_TIMEOUT_SECONDS) as client:
            response = client.post(
                f"{router_url}{SUBAGENT_PROBE_PATH}",
                headers={"X-AGW-Key": local_key},
                json=registration.to_dict(),
            )
    except httpx.HTTPError as exc:
        raise ProxyError(_REGISTRATION_FAILED) from exc

    if response.status_code != 201:
        raise ProxyError(_REGISTRATION_FAILED)
    try:
        body: Any = response.json()
    except ValueError as exc:
        raise ProxyError(_REGISTRATION_FAILED) from exc
    if body != {"ok": True, "expires_in": 60}:
        raise ProxyError(_REGISTRATION_FAILED)


def run_pre_tool_use_hook(
    context: HookContext,
    payload: dict[str, object],
) -> tuple[int, dict[str, object]]:
    """Validate the pending Agent call and record it when it fits the contract."""
    existing = read_probe_events(context.probe_dir, context.file)
    event = pre_tool_use_event(payload, context.expected_agent_type, existing)
    if event is None:
        return 0, pre_tool_use_decision(_DENY_REASON)
    append_probe_event(context.probe_dir, context.file, event)
    return 0, pre_tool_use_decision()


def run_subagent_start_hook(context: HookContext, payload: dict[str, object]) -> int:
    """Record the child, then register its route authorization before returning.

    Returning before registration succeeds would let the child's first model
    request reach the router unauthorized, and the probe would then fail for a
    reason unrelated to what it is measuring.
    """
    event = subagent_start_event(payload, context.expected_agent_type)
    existing = read_probe_events(context.probe_dir, context.file)
    if any(item.get("event") == "subagent_start" for item in existing):
        raise _failed()

    append_probe_event(context.probe_dir, context.file, event)
    register_probe(
        context.router_url,
        context.local_key,
        RouterProbeRegistration(
            nonce=context.nonce,
            agent_id=str(event["agent_id"]),
            model=context.expected_model,
            provider=context.expected_provider,
            file=context.file,
        ),
    )
    return 0


# --------------------------------------------------------------------------- #
# Verifier process construction
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ProbeContext:
    """One probe run: the hook's view plus the child's working directory.

    ``run_dir`` is the child's ``cwd``. It exists so ``--setting-sources local``
    resolves against a directory with no ``.claude`` tree, and so the only file
    the child can trivially reach is the fixture it is asked to read. Never
    serialized: it is passed as process state, not as evidence.
    """

    hook: HookContext
    run_dir: Path
    fixture_name: str


def build_verifier_agents(worker: GeneratedAgent) -> dict[str, dict[str, object]]:
    """The two inline definitions the probe runs with.

    The parent is a native Claude agent that can only call Agent; the worker is
    the generated external definition under test. Neither can do the other's job,
    so a passing run cannot be explained by the parent doing the read itself.
    """
    return {
        VERIFIER_PARENT_TYPE: {
            "description": "Run one controlled AGW native-subagent compatibility check.",
            "prompt": _VERIFIER_PROMPT,
            "model": "sonnet",
            "tools": ["Agent"],
            "maxTurns": 4,
        },
        worker.agent_type: worker.definition,
    }


def build_probe_settings(hook_command: str, worker_type: str) -> dict[str, object]:
    """Session-scope hooks only.

    Takes no environment and no secret, so the settings JSON — which is visible
    in the process argument list — cannot carry one. A worker-definition hook
    would be installed too late to observe the worker's own ``SubagentStart``,
    which is why these are session scope.
    """

    def command(subcommand: str) -> dict[str, object]:
        return {
            "type": "command",
            "command": hook_command,
            "args": ["-I", "-m", "agent_gateway.subagent_probe", "hook", subcommand],
            "timeout": HOOK_TIMEOUT_SECONDS,
        }

    return {
        "hooks": {
            "PreToolUse": [{"matcher": "Agent", "hooks": [command("pre-tool-use")]}],
            "SubagentStart": [{"matcher": worker_type, "hooks": [command("subagent-start")]}],
        }
    }


def build_probe_env(
    base_env: dict[str, str],
    context: ProbeContext,
    *,
    base_url: str,
) -> dict[str, str]:
    """The only place the hook variables and the local key are assembled."""
    env = dict(base_env)
    for name in _SCRUBBED_VARS:
        env.pop(name, None)
    hook = context.hook
    env.update(
        {
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_CUSTOM_HEADERS": f"X-AGW-Key: {hook.local_key}",
            "AGW_PROBE_DIR": str(hook.probe_dir),
            "AGW_PROBE_EVENT_FILE": hook.file.path,
            "AGW_PROBE_EVENT_DEVICE": str(hook.file.device),
            "AGW_PROBE_EVENT_INODE": str(hook.file.inode),
            "AGW_PROBE_EVENT_UID": str(hook.file.uid),
            "AGW_PROBE_EVENT_MODE": str(hook.file.mode),
            "AGW_PROBE_NONCE": hook.nonce,
            "AGW_PROBE_EXPECTED_AGENT_TYPE": hook.expected_agent_type,
            "AGW_PROBE_EXPECTED_MODEL": hook.expected_model,
            "AGW_PROBE_EXPECTED_PROVIDER": hook.expected_provider,
            "AGW_PROBE_ROUTER_URL": hook.router_url,
            "AGW_PROBE_LOCAL_KEY": hook.local_key,
        }
    )
    return env


def build_probe_prompt(worker_type: str, fixture_name: str) -> str:
    """The bounded initial prompt.

    ``fixture_name`` is a bare filename resolved against the child's ``cwd``, so
    the delegated prompt that reaches the external provider never contains AGW's
    state-directory layout.
    """
    return (
        f"Use Agent exactly once with subagent_type {worker_type}, omit model, and ask it "
        f"to read {fixture_name} and return the exact one-line content. Then return only "
        "that child result."
    )


def build_probe_argv(
    executable: Path,
    worker: GeneratedAgent,
    settings_json: str,
    agents_json: str,
    prompt: str,
) -> list[str]:
    """Non-interactive, budget-bounded, and isolated from user configuration.

    ``--safe-mode`` cannot be used here even though it would isolate more: it
    disables custom agents, which is the entire mechanism under test.
    """
    return [
        str(executable),
        "--print",
        "--output-format",
        "stream-json",
        # Required by the client: `--print` with stream-json refuses to start
        # without it.
        "--verbose",
        "--include-hook-events",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--no-chrome",
        "--strict-mcp-config",
        "--mcp-config",
        # Claude validates this payload, so a bare `{}` is rejected with
        # "mcpServers: expected record, received undefined". The empty config
        # has to name the (empty) server record explicitly.
        '{"mcpServers":{}}',
        "--tools",
        _SESSION_TOOLS,
        "--allowedTools",
        _SESSION_TOOLS,
        "--disallowedTools",
        _SESSION_DISALLOWED,
        "--setting-sources",
        "local",
        "--settings",
        settings_json,
        "--agents",
        agents_json,
        "--agent",
        VERIFIER_PARENT_TYPE,
        "--max-budget-usd",
        PROBE_BUDGET_USD,
        prompt,
    ]


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class NativeSubagentVerifyReport:
    """One model's compatibility verdict, sanitized for display."""

    model: str
    provider: str
    ok: bool
    detail: str


def probe_file_identity_from_env(env: dict[str, str]) -> ProbeFileIdentity:
    """The event-file identity a hook process would reconstruct."""
    return hook_context_from_env(env).file


def _texts(payload: Any) -> Iterator[str]:
    """Every string anywhere in a parsed stream object."""
    if isinstance(payload, str):
        yield payload
    elif isinstance(payload, dict):
        for value in payload.values():
            yield from _texts(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from _texts(value)


def _successful_results(stdout: str) -> list[str]:
    """Every successful result the run produced, in order.

    There can legitimately be more than one. Claude may spawn the child in the
    background, answer immediately ("it's running now"), and deliver the real
    content later as a second result carrying
    ``origin: {"kind": "task-notification"}``. Reading only the first would
    discard exactly the answer the probe is looking for.
    """
    results: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if not isinstance(payload, dict) or payload.get("type") != "result":
            continue
        if payload.get("subtype") != "success" or payload.get("is_error"):
            continue
        result = payload.get("result")
        if isinstance(result, str):
            results.append(result)
    return results


def _evaluate(
    events: tuple[dict[str, object], ...],
    stdout: str,
    returncode: int,
    context: ProbeContext,
    canary: str,
) -> str | None:
    """Return a sanitized failure detail, or ``None`` when the probe passed.

    Every branch returns a fixed phrase. Nothing derived from the model output,
    the delegated task, or the filesystem layout may end up in stored evidence.
    """
    # The exit code is deliberately *not* the outcome. Claude Code exits non-zero
    # merely for emitting `[claude-code:unrecognized_model]` about an external name
    # it does not know, even after routing that name, running the child on it, and
    # completing successfully. The final result object is authoritative; the exit
    # code only sharpens the message when there is no result to read.
    results = _successful_results(stdout)
    if not results:
        return (
            "native Claude subagent probe exited non-zero"
            if returncode != 0
            else "the probe produced no successful final result"
        )

    lowered = stdout.lower()
    if any(marker in lowered for marker in _SUBSTITUTION_MARKERS):
        # A warning about model restriction means Claude may have run its own
        # model instead; a plausible answer is then worth nothing.
        return "Claude reported a model restriction or substitution"

    if len(events) != 3:
        return "probe evidence is incomplete"
    pre_tool, start, route = events
    if pre_tool.get("event") != "pre_tool_use":
        return "probe evidence is out of order"
    if start.get("event") != "subagent_start":
        return "probe evidence is out of order"
    if route.get("event") != "route":
        return "probe evidence is out of order"

    hook = context.hook
    if pre_tool.get("subagent_type") != hook.expected_agent_type:
        return "the parent selected an unexpected subagent type"
    if pre_tool.get("explicit_model_absent") is not True:
        return "the parent passed an explicit Agent model override"

    agent_id = start.get("agent_id")
    if start.get("agent_type") != hook.expected_agent_type:
        return "an unexpected subagent type was started"
    if not isinstance(agent_id, str) or not agent_id:
        return "the started subagent had no id"

    if route.get("agent_id") != agent_id:
        return "no request from the started subagent reached the gateway"
    if route.get("nonce") != hook.nonce:
        return "route evidence belongs to a different probe"
    if route.get("requested_model") != hook.expected_model:
        return "the subagent requested a different model"
    if route.get("resolved_model") != hook.expected_model:
        return "the gateway resolved a different model"
    if route.get("provider") != hook.expected_provider:
        return "the gateway routed to a different provider"
    if route.get("request_kind") != "messages":
        return "the subagent never issued a messages request"

    # The fixture canary is how the read round trip is proven end to end: the
    # worker is the only agent with Read, so its content can only reach the
    # parent's final result by the worker having actually read the file.
    if not any(canary in result for result in results):
        return "the subagent did not complete a read round trip"

    return None


def verify_native_subagent(
    paths: Paths,
    config: GatewayConfig,
    registry: ModelRegistry,
    state: ProxyState,
    local_key: str,
    entry: ModelEntry,
    *,
    now: Callable[[], datetime] = utc_now,
    popen: PopenFactory = subprocess.Popen,
) -> NativeSubagentVerifyReport:
    """Run one bounded native-subagent compatibility probe for ``entry``.

    Returns a report for any ordinary verdict so a caller can attempt every
    requested model before failing. Only a broken probe — a registration or
    cleanup failure — raises.
    """
    del registry  # the caller resolved `entry`; kept for a stable signature

    identity = CompatibilityIdentity(
        model=entry.name,
        provider=entry.provider,
        claude=resolve_claude_identity(config),
        runtime_fingerprint=state.runtime_fingerprint,
        definition_contract=DEFINITION_CONTRACT_VERSION,
    )
    # Invalidate first: a probe that crashes or is cancelled must not leave a
    # previous success in place, still advertising an agent nobody is defending.
    invalidate_compatibility(paths, identity, now=now())

    worker = build_generated_agent(entry)
    nonce = secrets.token_hex(16)
    ensure_dir(paths.external_subagents_dir)
    ensure_dir(paths.probe_dir)
    file_identity = create_probe_file(paths.probe_dir, nonce)

    run_dir = ensure_dir(paths.probe_dir / f"run-{nonce}")
    canary = f"agw-probe-{secrets.token_hex(8)}"
    fixture_name = f"agw-fixture-{secrets.token_hex(4)}.txt"
    fixture = run_dir / fixture_name
    atomic_write_text(fixture, canary + "\n")

    context = ProbeContext(
        hook=HookContext(
            probe_dir=paths.probe_dir,
            file=file_identity,
            nonce=nonce,
            expected_agent_type=worker.agent_type,
            expected_model=entry.name,
            expected_provider=entry.provider.value,
            router_url=state.url,
            local_key=local_key,
        ),
        run_dir=run_dir,
        fixture_name=fixture_name,
    )

    try:
        detail = _run_probe(context, worker, config, state, popen)
    finally:
        _cleanup(paths, context)

    outcome: Literal["passed", "failed"] = "failed" if detail else "passed"
    final_detail = detail or "exact native subagent route verified"
    write_compatibility_record(
        paths,
        CompatibilityRecord(
            schema=COMPATIBILITY_SCHEMA,
            identity=identity,
            verified_at=now(),
            outcome=outcome,
            detail=final_detail,
        ),
    )
    return NativeSubagentVerifyReport(
        model=entry.name,
        provider=entry.provider.value,
        ok=detail is None,
        detail=final_detail,
    )


def _run_probe(
    context: ProbeContext,
    worker: GeneratedAgent,
    config: GatewayConfig,
    state: ProxyState,
    popen: PopenFactory,
) -> str | None:
    """Start the bounded child, join its evidence, and return a failure detail."""
    import os

    settings_json = json.dumps(
        build_probe_settings(sys.executable, worker.agent_type),
        sort_keys=True,
        separators=(",", ":"),
    )
    agents_json = json.dumps(
        build_verifier_agents(worker),
        sort_keys=True,
        separators=(",", ":"),
    )
    prompt = build_probe_prompt(worker.agent_type, context.fixture_name)
    executable = Path(str(config.native_claude_path))
    argv = build_probe_argv(executable, worker, settings_json, agents_json, prompt)
    env = build_probe_env(dict(os.environ), context, base_url=state.url)

    process = popen(
        argv,
        cwd=str(context.run_dir),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    process_identity = identity_of(process.pid)

    try:
        stdout, _stderr = process.communicate(timeout=PROBE_PROCESS_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        if process_identity is not None:
            terminate_process_group(
                process_identity,
                term_timeout=PROBE_TERM_TIMEOUT_SECONDS,
                kill_timeout=PROBE_KILL_TIMEOUT_SECONDS,
            )
        return "native Claude subagent probe timed out"
    except BaseException:
        # Cancellation must never be reported as anything but a failure.
        if process_identity is not None:
            terminate_process_group(
                process_identity,
                term_timeout=PROBE_TERM_TIMEOUT_SECONDS,
                kill_timeout=PROBE_KILL_TIMEOUT_SECONDS,
            )
        raise

    canary = (context.run_dir / context.fixture_name).read_text(encoding="utf-8").strip()
    try:
        events = read_probe_events(context.hook.probe_dir, context.hook.file)
    except ProxyError:
        return "probe evidence is unreadable"
    return _evaluate(
        events,
        (stdout or "")[:_MAX_PROBE_OUTPUT_BYTES],
        int(process.returncode or 0),
        context,
        canary,
    )


def _cleanup(paths: Paths, context: ProbeContext) -> None:
    """Remove every probe artifact, on success and on failure alike.

    Removing the event file also disarms any late writer: a registration that
    somehow survives can no longer append anything that would satisfy a probe.
    """
    import shutil

    try:
        remove_probe_file(paths.probe_dir, context.hook.file)
        shutil.rmtree(context.run_dir, ignore_errors=False)
    except (OSError, ProxyError) as exc:
        raise ProxyError("could not clean native subagent probe state.") from exc


def hook_main(
    argv: list[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Entry point for ``python -m agent_gateway.subagent_probe hook <event>``."""
    import os

    args = list(sys.argv[1:] if argv is None else argv)
    source = sys.stdin if stdin is None else stdin
    sink = sys.stdout if stdout is None else stdout

    try:
        if len(args) != 2 or args[0] != "hook":
            raise _failed()
        raw = source.read(_MAX_HOOK_INPUT_BYTES + 1)
        if len(raw) > _MAX_HOOK_INPUT_BYTES:
            raise _failed()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise _failed()

        context = hook_context_from_env(dict(os.environ))
        if args[1] == "pre-tool-use":
            code, response = run_pre_tool_use_hook(context, payload)
            sink.write(json.dumps(response))
            return code
        if args[1] == "subagent-start":
            return run_subagent_start_hook(context, payload)
        raise _failed()
    except (ProxyError, ValueError, OSError):
        # Nothing from the payload, environment, or response reaches stderr: the
        # verifier decides the outcome from evidence, not from hook chatter.
        print(_HOOK_FAILED, file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(hook_main())
