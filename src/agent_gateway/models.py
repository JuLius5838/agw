"""Model listing and Claude-compatibility verification.

``list_models`` renders the active public model set (plus inactive candidates with
``--all``) as a table or a stable JSON document for the plugin skill.

``verify_models`` sends bounded requests through the running proxy's Anthropic
Messages endpoint to confirm a model is actually usable by Claude Code: token
counting, required version/beta headers, system content, a clean SSE stream, and a
tool-use / tool-result round trip. A missing, unauthorized, renamed, or
tool-incompatible model fails under its own public name — the gateway never falls
back to another model.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import StrEnum

import httpx

from agent_gateway.errors import ModelUnavailableError
from agent_gateway.model_registry import ModelEntry, ModelRegistry
from agent_gateway.proxy import ProxyState

_ANTHROPIC_VERSION = "2023-06-01"
_VERIFY_TIMEOUT = 60.0


@dataclass(frozen=True)
class ModelListItem:
    name: str
    provider: str
    mode: str
    enabled: bool
    is_default: bool


def list_models(registry: ModelRegistry, *, include_inactive: bool = False) -> list[ModelListItem]:
    entries: list[ModelEntry] = list(registry.active_models())
    if include_inactive:
        entries += list(registry.inactive_models())
    return [
        ModelListItem(
            name=e.name,
            provider=e.provider.value,
            mode=e.mode.value,
            enabled=e.enabled,
            is_default=(e.name == registry.default_model and e.enabled),
        )
        for e in entries
    ]


def list_models_json(registry: ModelRegistry, *, include_inactive: bool = False) -> str:
    items = [asdict(item) for item in list_models(registry, include_inactive=include_inactive)]
    return json.dumps({"default_model": registry.default_model, "models": items}, indent=2)


def render_table(items: list[ModelListItem]) -> str:
    if not items:
        return "(no models)"
    header = f"{'MODEL':<28} {'PROVIDER':<10} {'MODE':<10} {'ACTIVE':<7} DEFAULT"
    rows = [header]
    for item in items:
        rows.append(
            f"{item.name:<28} {item.provider:<10} {item.mode:<10} "
            f"{'yes' if item.enabled else 'no':<7} {'*' if item.is_default else ''}"
        )
    return "\n".join(rows)


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #
class CheckStatus(StrEnum):
    passed = "passed"
    failed = "failed"


@dataclass(frozen=True)
class Check:
    name: str
    status: CheckStatus
    detail: str


@dataclass(frozen=True)
class VerifyReport:
    model: str
    provider: str
    ok: bool
    checks: list[Check]


def _headers(key: str) -> dict[str, str]:
    return {
        "X-AGW-Key": key,
        "anthropic-version": _ANTHROPIC_VERSION,
        "content-type": "application/json",
    }


def _messages_url(base: str) -> str:
    return f"{base}/v1/messages"


def _count_tokens(client: httpx.Client, base: str, key: str, model: str) -> Check:
    try:
        resp = client.post(
            f"{base}/v1/messages/count_tokens",
            headers=_headers(key),
            json={"model": model, "messages": [{"role": "user", "content": "ping"}]},
            timeout=_VERIFY_TIMEOUT,
        )
        if resp.status_code == 200 and "input_tokens" in resp.json():
            return Check("count_tokens", CheckStatus.passed, "token counting works")
        return Check("count_tokens", CheckStatus.failed, f"status {resp.status_code}")
    except (httpx.HTTPError, ValueError) as exc:
        return Check("count_tokens", CheckStatus.failed, f"error: {type(exc).__name__}")


def _non_streaming(client: httpx.Client, base: str, key: str, model: str) -> Check:
    try:
        resp = client.post(
            _messages_url(base),
            headers=_headers(key),
            json={
                "model": model,
                "max_tokens": 16,
                "system": "You are a terse assistant.",
                "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
            },
            timeout=_VERIFY_TIMEOUT,
        )
        if resp.status_code != 200:
            return Check("messages", CheckStatus.failed, f"status {resp.status_code}")
        body = resp.json()
        if body.get("type") == "message" and body.get("content"):
            return Check("messages", CheckStatus.passed, "system + non-streaming reply ok")
        return Check("messages", CheckStatus.failed, "unexpected response shape")
    except (httpx.HTTPError, ValueError) as exc:
        return Check("messages", CheckStatus.failed, f"error: {type(exc).__name__}")


def _streaming(client: httpx.Client, base: str, key: str, model: str) -> Check:
    try:
        with client.stream(
            "POST",
            _messages_url(base),
            headers=_headers(key),
            json={
                "model": model,
                "max_tokens": 16,
                "stream": True,
                "messages": [{"role": "user", "content": "Count: 1 2 3"}],
            },
            timeout=_VERIFY_TIMEOUT,
        ) as resp:
            if resp.status_code != 200:
                return Check("streaming", CheckStatus.failed, f"status {resp.status_code}")
            saw_start = saw_stop = False
            for line in resp.iter_lines():
                if "message_start" in line:
                    saw_start = True
                if "message_stop" in line:
                    saw_stop = True
        if saw_start and saw_stop:
            return Check("streaming", CheckStatus.passed, "clean SSE start/stop")
        return Check("streaming", CheckStatus.failed, "incomplete SSE termination")
    except (httpx.HTTPError, ValueError) as exc:
        return Check("streaming", CheckStatus.failed, f"error: {type(exc).__name__}")


def _tool_use(client: httpx.Client, base: str, key: str, model: str) -> Check:
    tool = {
        "name": "get_weather",
        "description": "Get the weather for a city.",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    }
    try:
        resp = client.post(
            _messages_url(base),
            headers=_headers(key),
            json={
                "model": model,
                "max_tokens": 256,
                "tools": [tool],
                "messages": [{"role": "user", "content": "Use get_weather for Paris."}],
            },
            timeout=_VERIFY_TIMEOUT,
        )
        if resp.status_code != 200:
            return Check("tool_use", CheckStatus.failed, f"status {resp.status_code}")
        blocks = resp.json().get("content", [])
        if any(isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks):
            return Check("tool_use", CheckStatus.passed, "emitted a tool_use block")
        return Check("tool_use", CheckStatus.failed, "no tool_use block produced")
    except (httpx.HTTPError, ValueError) as exc:
        return Check("tool_use", CheckStatus.failed, f"error: {type(exc).__name__}")


def verify_model(state: ProxyState, key: str, entry: ModelEntry) -> VerifyReport:
    """Run the bounded Claude-compatibility checks for one model."""
    checks: list[Check] = []
    with httpx.Client() as client:
        checks.append(_count_tokens(client, state.url, key, entry.name))
        checks.append(_non_streaming(client, state.url, key, entry.name))
        checks.append(_streaming(client, state.url, key, entry.name))
        checks.append(_tool_use(client, state.url, key, entry.name))
    ok = all(c.status is CheckStatus.passed for c in checks)
    return VerifyReport(model=entry.name, provider=entry.provider.value, ok=ok, checks=checks)


def resolve_models_to_verify(registry: ModelRegistry, model: str | None) -> list[ModelEntry]:
    """Return the entries to verify: one named model, or every active model."""
    if model is not None:
        return [registry.get_active(model)]  # raises ModelUnavailableError if not active
    active = list(registry.active_models())
    if not active:
        raise ModelUnavailableError("no active models to verify.")
    return active
