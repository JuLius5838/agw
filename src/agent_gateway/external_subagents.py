"""Generated native Claude agent definitions for active external models.

Claude Code's built-in Agent tool validates its explicit ``model`` argument
against a Claude-only enum, so an external AGW model can never be chosen per
invocation. A custom-agent *definition* carries its own ``model`` field, which
Claude resolves when it spawns the child; this module builds one such definition
per eligible external model so the parent session selects only ``subagent_type``
and omits the enum-constrained override entirely.

Everything here is pure — no filesystem, network, or process state — so the
launcher can build definitions cheaply on every managed launch. Routing always
uses the exact public :attr:`ModelEntry.name`; the readable half of a generated
agent id is diagnostic only and is never a routing alias.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from agent_gateway.claude_compat import (
    ClaudeExecutableIdentity,
    CompatibilityIdentity,
    CompatibilityState,
    CompatibilityStatus,
    compatibility_status,
)
from agent_gateway.errors import ConfigError
from agent_gateway.model_registry import ModelEntry, ModelRegistry
from agent_gateway.paths import Paths
from agent_gateway.providers import Provider

# Bump only with a deliberate change to the emitted definition shape: the version
# is part of the identity digest, so a bump invalidates existing compatibility
# evidence instead of silently reusing it for a different contract.
DEFINITION_CONTRACT_VERSION: Final[int] = 1

# The complete v1 allowlist. Claude Code treats an explicit built-in list as the
# whole tool set, so omitting Agent/Workflow prevents recursion and omitting
# Bash/Edit/Write keeps a generated external agent analysis-only.
EXTERNAL_AGENT_TOOLS: Final[tuple[str, ...]] = ("Read", "Grep", "Glob")
EXTERNAL_AGENT_MAX_TURNS: Final[int] = 20

_AGENTS_OPTION: Final[str] = "--agents"
_OPTION_TERMINATOR: Final[str] = "--"
_AGENT_ID_PREFIX: Final[str] = "agw-"
_MAX_READABLE_PREFIX: Final[int] = 40
_DIGEST_CHARACTERS: Final[int] = 12
_NON_ALNUM: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]+")

_PROVIDER_LABELS: Final[dict[Provider, str]] = {
    Provider.chatgpt: "ChatGPT",
    Provider.copilot: "GitHub Copilot",
}

# Claude picks agents from their descriptions, so the description *is* the
# orchestration policy: it must name the provider, disclose that repository
# content leaves the machine, invite parallel use, and discourage waste.
_DESCRIPTION_TEMPLATE: Final[str] = (
    "Use the external AGW model {model} ({provider_label}) for an independent "
    "read-only perspective. Repository content read by this agent is sent to "
    "{provider_label}. Multiple AGW model agents may run concurrently when their "
    "perspectives add value; do not use merely to spend quota or duplicate work."
)

_PROMPT: Final[str] = (
    "Complete only the delegated analysis task. Use only the provided read-only "
    "repository tools. Do not attempt to modify files or invoke external services. "
    "State limitations clearly and return a concise result to the parent."
)


@dataclass(frozen=True)
class GeneratedAgent:
    """One external model expressed as a Claude Code custom-agent definition."""

    agent_type: str
    model: str
    provider: Provider
    definition: dict[str, object]


def _sanitized_prefix(model: str) -> str:
    """Readable, diagnostic-only slug for a public model name.

    Lowercase, collapse every maximal non-alphanumeric run to a single hyphen,
    trim, and truncate. Truncation can land mid-separator, so the trim is
    repeated afterwards to avoid an id ending in a hyphen. Distinct public names
    can legitimately collapse to the same slug — the digest, not this prefix, is
    what keeps generated ids unique.
    """
    collapsed = _NON_ALNUM.sub("-", model.lower()).strip("-")
    truncated = collapsed[:_MAX_READABLE_PREFIX].strip("-")
    return truncated or "model"


def _identity_digest(entry: ModelEntry) -> str:
    """First 12 hex characters of SHA-256 over canonical identity JSON.

    A 48-bit suffix is a deterministic namespace, not a security boundary; it
    exists so two public names that sanitize alike still get distinct agent ids.
    """
    material = json.dumps(
        {
            "contract": DEFINITION_CONTRACT_VERSION,
            "model": entry.name,
            "provider": entry.provider.value,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:_DIGEST_CHARACTERS]


def generated_agent_type(entry: ModelEntry) -> str:
    """The reserved ``agw-`` agent id for one exact model/provider pair."""
    return f"{_AGENT_ID_PREFIX}{_sanitized_prefix(entry.name)}-{_identity_digest(entry)}"


def build_generated_agent(entry: ModelEntry) -> GeneratedAgent:
    """Build the exact v1 definition for one external model."""
    label = _PROVIDER_LABELS[entry.provider]
    definition: dict[str, object] = {
        "description": _DESCRIPTION_TEMPLATE.format(model=entry.name, provider_label=label),
        "prompt": _PROMPT,
        "model": entry.name,
        "tools": list(EXTERNAL_AGENT_TOOLS),
        "maxTurns": EXTERNAL_AGENT_MAX_TURNS,
    }
    return GeneratedAgent(
        agent_type=generated_agent_type(entry),
        model=entry.name,
        provider=entry.provider,
        definition=definition,
    )


def build_generated_agents(entries: Iterable[ModelEntry]) -> tuple[GeneratedAgent, ...]:
    """Build definitions in a stable order, refusing any id collision."""
    agents = sorted(
        (build_generated_agent(entry) for entry in entries),
        key=lambda agent: (agent.model, agent.provider.value, agent.agent_type),
    )
    seen: set[str] = set()
    for agent in agents:
        if agent.agent_type in seen:
            raise ConfigError(f"generated external-agent IDs collide: '{agent.agent_type}'.")
        seen.add(agent.agent_type)
    return tuple(agents)


# --------------------------------------------------------------------------- #
# ``--agents`` launch-argument merge
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _UserOption:
    """One recognized pre-terminator ``--agents`` occurrence.

    ``start``/``stop`` bound the tokens the option occupies, so the merged option
    can be emitted at the user's original position regardless of whether they
    wrote the two-token or the inline ``=`` form.
    """

    start: int
    stop: int
    value: str


def _scan_user_agents(args: Sequence[str]) -> tuple[_UserOption | None, int | None]:
    """Locate the single pre-terminator ``--agents`` option and the terminator.

    Everything at or after the first bare ``--`` is positional prompt content and
    is never interpreted, so the scan stops there and reports its index.
    """
    option: _UserOption | None = None
    index = 0
    while index < len(args):
        token = args[index]
        if token == _OPTION_TERMINATOR:
            return option, index
        if token == _AGENTS_OPTION:
            if index + 1 >= len(args) or args[index + 1] == _OPTION_TERMINATOR:
                raise ConfigError(
                    "--agents requires a JSON object value before '--'.",
                    hint="Pass the definition object as `--agents '{\"id\": {...}}'`.",
                )
            found = _UserOption(start=index, stop=index + 2, value=args[index + 1])
            index += 2
        elif token.startswith(f"{_AGENTS_OPTION}="):
            found = _UserOption(
                start=index,
                stop=index + 1,
                value=token[len(_AGENTS_OPTION) + 1 :],
            )
            index += 1
        else:
            # A longer option such as `--agents-file` merely shares a prefix.
            index += 1
            continue
        if option is not None:
            raise ConfigError(
                "--agents may be specified at most once before '--'.",
                hint="Claude Code's precedence for repeated options is unspecified; merge them.",
            )
        option = found
    return option, None


def _parse_user_definitions(value: str) -> dict[str, object]:
    """Parse the user's object, never echoing it into a diagnostic.

    Custom definitions routinely carry private prompts and server configuration,
    so failures name only the option.
    """
    try:
        parsed = json.loads(value)
    except ValueError:
        raise ConfigError(
            "--agents must contain valid JSON; the value was not included in this diagnostic.",
            hint="Custom definitions can carry sensitive prompts, so the value is not echoed.",
        ) from None
    if not isinstance(parsed, dict):
        raise ConfigError(
            "--agents JSON must have an object root.",
            hint="Pass an object mapping each agent id to its definition.",
        )
    return {str(key): item for key, item in parsed.items()}


def merge_agents_args(
    forwarded_args: Sequence[str],
    generated: Sequence[GeneratedAgent],
) -> list[str]:
    """Return launch arguments carrying both user and AGW agent definitions.

    With no generated definitions the arguments are forwarded byte-for-byte and
    are not even parsed: AGW has nothing to add, so a malformed user value stays
    Claude's business rather than becoming an AGW launch failure.
    """
    args = list(forwarded_args)
    if not generated:
        return args

    option, terminator = _scan_user_agents(args)
    definitions = _parse_user_definitions(option.value) if option is not None else {}
    for agent in generated:
        if agent.agent_type in definitions:
            raise ConfigError(
                f"--agents defines reserved generated agent ID '{agent.agent_type}'.",
                hint="Rename your definition; AGW never overwrites a user-supplied agent.",
            )
        definitions[agent.agent_type] = agent.definition

    payload = json.dumps(definitions, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if option is not None:
        return [*args[: option.start], _AGENTS_OPTION, payload, *args[option.stop :]]
    # Appending past a `--` would turn the option into prompt text, so the
    # generated option goes immediately before the terminator when one exists.
    insert_at = terminator if terminator is not None else len(args)
    return [*args[:insert_at], _AGENTS_OPTION, payload, *args[insert_at:]]


# --------------------------------------------------------------------------- #
# Launch-time eligibility
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Eligibility:
    """One active model's advertisement decision, with the reason attached.

    ``agent`` is non-``None`` only for ``current`` evidence. The status travels
    with it so ``doctor`` can explain an omission without recomputing it.
    """

    agent: GeneratedAgent | None
    status: CompatibilityStatus


def resolve_external_agent_eligibility(
    paths: Paths,
    registry: ModelRegistry,
    claude: ClaudeExecutableIdentity,
    runtime_fingerprint: str,
    settings: Sequence[tuple[Path, dict[str, object]]],
) -> tuple[Eligibility, ...]:
    """Decide, per active model, whether AGW may advertise a generated agent.

    Provider authentication is deliberately *not* consulted here: it stays a
    global, fail-fast proxy-startup invariant, so an unauthenticated provider
    stops the launch outright rather than silently dropping one model.
    """
    rows: list[Eligibility] = []
    for entry in registry.active_models():
        identity = CompatibilityIdentity(
            model=entry.name,
            provider=entry.provider,
            claude=claude,
            runtime_fingerprint=runtime_fingerprint,
            definition_contract=DEFINITION_CONTRACT_VERSION,
        )
        status = compatibility_status(paths, identity, settings=settings)
        agent = (
            build_generated_agent(entry) if status.state is CompatibilityState.current else None
        )
        rows.append(Eligibility(agent=agent, status=status))
    return tuple(rows)


def has_any_compatibility_evidence(paths: Paths) -> bool:
    """Cheap check for whether any model has ever been verified.

    Resolving the Claude identity costs a ``claude --version`` subprocess and a
    full SHA-256 of the binary. Without a single stored record nothing can be
    ``current``, so an ordinary launch skips that work entirely rather than
    paying it on every session.
    """
    directory = paths.compatibility_dir
    if not directory.is_dir():
        return False
    return any(path.is_file() for path in directory.glob("*/*.json"))


def current_generated_agents(
    paths: Paths,
    registry: ModelRegistry,
    claude: ClaudeExecutableIdentity,
    runtime_fingerprint: str,
    settings: Sequence[tuple[Path, dict[str, object]]],
) -> tuple[GeneratedAgent, ...]:
    """Just the definitions a managed launch may inject."""
    return tuple(
        row.agent
        for row in resolve_external_agent_eligibility(
            paths, registry, claude, runtime_fingerprint, settings
        )
        if row.agent is not None
    )


def inspect_generated_id_collisions(
    settings: Sequence[tuple[Path, dict[str, object]]],
    agents: Sequence[GeneratedAgent],
) -> tuple[tuple[Path, str], ...]:
    """Report inspectable settings that claim an AGW-reserved agent id.

    Only a top-level ``agents`` object is inspected, and nothing is modified.
    Higher-priority managed definitions cannot be enumerated this way at all —
    those are caught by the live probe, not here.
    """
    reserved = {agent.agent_type for agent in agents}
    collisions: list[tuple[Path, str]] = []
    for path, payload in settings:
        declared = payload.get("agents")
        if not isinstance(declared, dict):
            continue
        for name in declared:
            if name in reserved:
                collisions.append((path, str(name)))
    return tuple(collisions)
