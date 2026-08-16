"""Unit tests for the hybrid front router's routing and credential boundary."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_gateway.errors import ProxyError
from agent_gateway.model_registry import load_registry_text
from agent_gateway.paths import ensure_dir
from agent_gateway.probe_events import create_probe_file, read_probe_events
from agent_gateway.providers import Provider
from agent_gateway.router import (
    SUBAGENT_PROBE_PATH,
    SUBAGENT_PROBE_TTL_SECONDS,
    OneShotProbeRegistry,
    ProbeRegistration,
    RouterSettings,
    create_app,
)

REGISTRY = load_registry_text(
    """
    default_model: null
    models:
      - name: gpt-5.6-sol
        display_name: Codex Sol
        provider: chatgpt
        upstream_model: chatgpt/gpt-5.6-sol
        mode: responses
        enabled: true
    """
)


async def _request(
    handler: Callable[[httpx.Request], httpx.Response],
    method: str,
    url: str,
    *,
    probe_registry: OneShotProbeRegistry | None = None,
    peer: tuple[str, int] = ("127.0.0.1", 50000),
    **kwargs: Any,
) -> httpx.Response:
    app = create_app(
        RouterSettings(
            registry=REGISTRY,
            local_key="local-key",
            internal_key="internal-key",
            litellm_url="http://litellm.invalid",
            anthropic_url="https://anthropic.invalid",
            transport=httpx.MockTransport(handler),
            probe_registry=probe_registry,
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=peer),
        base_url="http://router.test",
    ) as client:
        return await client.request(method, url, **kwargs)


def _send(
    handler: Callable[[httpx.Request], httpx.Response],
    method: str,
    url: str,
    **kwargs: Any,
) -> httpx.Response:
    return asyncio.run(_request(handler, method, url, **kwargs))


def test_models_are_hidden_ids_with_configured_display_names():
    handler = lambda _request: httpx.Response(200)  # noqa: E731 - healthy private backend
    unauthorized = _send(handler, "GET", "/v1/models")
    response = _send(handler, "GET", "/v1/models", headers={"X-AGW-Key": "local-key"})

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    assert response.json()["data"] == [
        {
            "id": "anthropic.agw.chatgpt.gpt-5.6-sol",
            "display_name": "Codex Sol",
            "type": "model",
            "object": "model",
            "owned_by": "agent-gateway",
        }
    ]


def test_models_wait_for_private_backend_readiness():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(503)

    response = _send(handler, "GET", "/v1/models", headers={"X-AGW-Key": "local-key"})

    assert response.status_code == 503
    assert response.json()["error"]["message"] == "external model backend is starting"
    assert len(seen) == 1
    assert seen[0].url == httpx.URL("http://litellm.invalid/health/readiness")
    assert seen[0].headers["authorization"] == "Bearer internal-key"


def test_native_request_preserves_auth_beta_and_raw_body():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "application/json"}, content=b"native")

    body = b'{"model":"claude-sonnet-4-5","messages":[]}'
    response = _send(
        handler,
        "POST",
        "/v1/messages?beta=true",
        content=body,
        headers={
            "X-AGW-Key": "local-key",
            "Authorization": "Bearer claude-oauth",
            "anthropic-beta": "fine-grained-tool-streaming-2025-05-14",
        },
    )

    assert response.status_code == 200
    assert response.content == b"native"
    assert len(seen) == 1
    assert seen[0].url == httpx.URL("https://anthropic.invalid/v1/messages?beta=true")
    assert seen[0].content == body
    assert seen[0].headers["authorization"] == "Bearer claude-oauth"
    assert seen[0].headers["anthropic-beta"] == "fine-grained-tool-streaming-2025-05-14"
    assert "x-agw-key" not in seen[0].headers


def test_native_request_removes_only_empty_unsigned_thinking_blocks() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"native")

    response = _send(
        handler,
        "POST",
        "/v1/messages",
        json={
            "model": "claude-opus-4-8",
            "messages": [
                {"role": "user", "content": "start"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "", "signature": ""},
                        {"type": "text", "text": "GPT answer"},
                    ],
                },
                {"role": "user", "content": "continue with Claude"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "",
                            "signature": "valid-claude-signature",
                        },
                        {"type": "redacted_thinking", "data": "encrypted"},
                        {
                            "type": "thinking",
                            "thinking": "visible summary",
                            "signature": "",
                        },
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "Read",
                            "input": {},
                        },
                    ],
                },
            ],
        },
        headers={"X-AGW-Key": "local-key"},
    )

    assert response.status_code == 200
    forwarded = json.loads(seen[0].content)
    first_assistant = forwarded["messages"][1]["content"]
    assert first_assistant == [{"type": "text", "text": "GPT answer"}]
    preserved = forwarded["messages"][3]["content"]
    assert [block["type"] for block in preserved] == [
        "thinking",
        "redacted_thinking",
        "thinking",
        "tool_use",
    ]
    assert preserved[0]["signature"] == "valid-claude-signature"


def test_native_request_drops_assistant_message_left_empty_by_cleanup() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"native")

    response = _send(
        handler,
        "POST",
        "/v1/messages/count_tokens",
        json={
            "model": "claude-opus-4-8",
            "messages": [
                {"role": "user", "content": "start"},
                {
                    "role": "assistant",
                    "content": [{"type": "thinking", "thinking": "", "signature": ""}],
                },
                {"role": "user", "content": "continue"},
            ],
        },
        headers={"X-AGW-Key": "local-key"},
    )

    assert response.status_code == 200
    forwarded = json.loads(seen[0].content)
    assert forwarded["messages"] == [
        {"role": "user", "content": "start"},
        {"role": "user", "content": "continue"},
    ]


def test_external_request_rewrites_picker_id_and_never_leaks_claude_auth():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, content=b"external")

    response = _send(
        handler,
        "POST",
        "/v1/messages",
        json={"model": "anthropic.agw.chatgpt.gpt-5.6-sol", "messages": []},
        headers={
            "X-AGW-Key": "local-key",
            "Authorization": "Bearer claude-oauth",
            "x-api-key": "claude-api-key",
        },
    )

    assert response.status_code == 201
    assert len(seen) == 1
    assert seen[0].url == httpx.URL("http://litellm.invalid/v1/messages")
    assert seen[0].headers["authorization"] == "Bearer internal-key"
    assert "x-api-key" not in seen[0].headers
    assert "x-agw-key" not in seen[0].headers
    assert json.loads(seen[0].content)["model"] == "gpt-5.6-sol"


def test_chatgpt_request_converts_hosted_search_to_client_tool() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"external")

    response = _send(
        handler,
        "POST",
        "/v1/messages",
        json={
            "model": "gpt-5.6-sol",
            "messages": [],
            "tools": [
                {
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": 8,
                },
                {
                    "name": "Read",
                    "description": "Read a file",
                    "input_schema": {"type": "object"},
                },
            ],
        },
        headers={"X-AGW-Key": "local-key"},
    )

    assert response.status_code == 200
    forwarded = json.loads(seen[0].content)
    assert forwarded["tools"][0]["name"] == "WebSearch"
    assert forwarded["tools"][0]["input_schema"]["required"] == ["query"]
    assert "type" not in forwarded["tools"][0]
    assert forwarded["tools"][1]["name"] == "Read"


def test_unknown_model_uses_native_path_and_upstream_status_is_preserved():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "2"}, content=b"busy")

    response = _send(
        handler,
        "POST",
        "/v1/messages",
        json={"model": "future-claude-model", "messages": []},
        headers={"X-AGW-Key": "local-key"},
    )

    assert response.status_code == 429
    assert response.headers["retry-after"] == "2"
    assert response.content == b"busy"


# --------------------------------------------------------------------------- #
# One-shot probe registry
# --------------------------------------------------------------------------- #
NONCE = "0" * 32
AGENT_ID = "agent-1"


class _Clock:
    """A monotonic clock the test drives explicitly."""

    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def probe_dir(tmp_path) -> Path:
    return ensure_dir(tmp_path / "probes", 0o700)


@pytest.fixture
def probe_identity(probe_dir):
    return create_probe_file(probe_dir, NONCE)


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def probe_registry(probe_dir, clock) -> OneShotProbeRegistry:
    return OneShotProbeRegistry(probe_dir, clock=clock)


def _registration(identity, **overrides):
    fields: dict[str, Any] = {
        "agent_id": AGENT_ID,
        "model": "gpt-5.6-sol",
        "provider": Provider.chatgpt,
        "nonce": NONCE,
        "file": identity,
        "expires_at": 160.0,
    }
    fields.update(overrides)
    return ProbeRegistration(**fields)


def _consume(registry: OneShotProbeRegistry, **overrides) -> bool:
    fields: dict[str, Any] = {
        "agent_id": AGENT_ID,
        "requested_model": "gpt-5.6-sol",
        "resolved_model": "gpt-5.6-sol",
        "provider": Provider.chatgpt,
        "request_kind": "messages",
        "timestamp": "2026-08-14T12:00:00Z",
    }
    fields.update(overrides)
    return registry.consume(**fields)


def test_probe_registration_is_exact_and_one_shot(probe_registry, probe_identity):
    probe_registry.register(_registration(probe_identity, expires_at=160.0))
    assert _consume(probe_registry) is True
    assert _consume(probe_registry) is False


def test_consumed_registration_writes_the_exact_route_event(
    probe_registry, probe_identity, probe_dir
):
    probe_registry.register(_registration(probe_identity))
    assert _consume(probe_registry) is True
    assert read_probe_events(probe_dir, probe_identity) == (
        {
            "schema": 1,
            "event": "route",
            "nonce": NONCE,
            "agent_id": AGENT_ID,
            "requested_model": "gpt-5.6-sol",
            "resolved_model": "gpt-5.6-sol",
            "provider": "chatgpt",
            "request_kind": "messages",
            "timestamp": "2026-08-14T12:00:00Z",
        },
    )


@pytest.mark.parametrize(
    "mismatch",
    [
        {"agent_id": "agent-2"},
        {"requested_model": "gpt-5.6-luna"},
        {"resolved_model": "gpt-5.6-luna"},
        {"provider": Provider.copilot},
        {"agent_id": ""},
    ],
    ids=["agent-id", "requested-model", "resolved-model", "provider", "missing-header"],
)
def test_a_mismatch_neither_matches_nor_consumes(
    probe_registry, probe_identity, probe_dir, mismatch
):
    probe_registry.register(_registration(probe_identity))
    assert _consume(probe_registry, **mismatch) is False
    assert read_probe_events(probe_dir, probe_identity) == ()
    # The registration must survive so the real request can still satisfy it.
    assert _consume(probe_registry) is True


def test_an_expired_registration_cannot_be_consumed(probe_registry, probe_identity, clock):
    probe_registry.register(_registration(probe_identity, expires_at=160.0))
    clock.now = 160.0
    assert _consume(probe_registry) is False


def test_expired_registrations_are_dropped_on_register(probe_registry, probe_identity, clock):
    probe_registry.register(_registration(probe_identity, expires_at=160.0))
    clock.now = 200.0
    other = create_probe_file(probe_registry.probe_dir, "1" * 32)
    probe_registry.register(
        _registration(other, agent_id="agent-2", nonce="1" * 32, expires_at=260.0)
    )
    assert probe_registry.live_count() == 1


@pytest.mark.parametrize("duplicate", ["nonce", "agent"], ids=["nonce", "agent-id"])
def test_duplicate_registrations_are_refused(probe_registry, probe_identity, duplicate):
    probe_registry.register(_registration(probe_identity))
    other = create_probe_file(probe_registry.probe_dir, "1" * 32)
    overrides = (
        {"agent_id": "agent-2", "file": other}
        if duplicate == "nonce"
        else {"nonce": "1" * 32, "file": other}
    )
    with pytest.raises(ValueError):
        probe_registry.register(_registration(probe_identity, **overrides))


def test_the_registry_is_capped(probe_registry, probe_dir):
    for index in range(32):
        nonce = f"{index:032x}"
        identity = create_probe_file(probe_dir, nonce)
        probe_registry.register(
            _registration(identity, agent_id=f"agent-{index}", nonce=nonce)
        )
    overflow = create_probe_file(probe_dir, f"{99:032x}")
    with pytest.raises(ProxyError, match="too many active subagent probe registrations."):
        probe_registry.register(
            _registration(overflow, agent_id="agent-overflow", nonce=f"{99:032x}")
        )


def test_a_failed_append_drops_the_registration_and_fails_closed(
    probe_registry, probe_identity, probe_dir
):
    probe_registry.register(_registration(probe_identity))
    Path(probe_identity.path).unlink()
    with pytest.raises(ProxyError):
        _consume(probe_registry)
    assert probe_registry.live_count() == 0


# --------------------------------------------------------------------------- #
# Control endpoint
# --------------------------------------------------------------------------- #
def _body(identity, **overrides) -> dict[str, Any]:
    payload: dict[str, Any] = {
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
    payload.update(overrides)
    return payload


def _register(probe_registry, identity, *, headers=None, peer=("127.0.0.1", 50000), **overrides):
    handler = lambda _request: httpx.Response(200)  # noqa: E731 - unused upstream
    return asyncio.run(
        _request(
            handler,
            "POST",
            SUBAGENT_PROBE_PATH,
            probe_registry=probe_registry,
            peer=peer,
            json=_body(identity, **overrides),
            headers={"X-AGW-Key": "local-key"} if headers is None else headers,
        )
    )


def test_probe_control_registers_a_loopback_authenticated_request(probe_registry, probe_identity):
    response = _register(probe_registry, probe_identity)
    assert response.status_code == 201
    assert response.json() == {"ok": True, "expires_in": 60}
    assert probe_registry.live_count() == 1
    assert SUBAGENT_PROBE_TTL_SECONDS == 60.0


@pytest.mark.parametrize("headers", [{}, {"X-AGW-Key": "wrong"}], ids=["missing", "wrong"])
def test_probe_control_requires_the_local_key(probe_registry, probe_identity, headers):
    assert _register(probe_registry, probe_identity, headers=headers).status_code == 401
    assert probe_registry.live_count() == 0


def test_probe_control_refuses_a_non_loopback_peer(probe_registry, probe_identity):
    response = _register(probe_registry, probe_identity, peer=("198.51.100.2", 50000))
    assert response.status_code == 403
    assert probe_registry.live_count() == 0


def test_probe_control_is_unavailable_without_a_registry(probe_identity):
    assert _register(None, probe_identity).status_code == 503


@pytest.mark.parametrize(
    "overrides",
    [
        {"nonce": "not-hex"},
        {"nonce": "A" * 32},
        {"agent_id": ""},
        {"agent_id": "x" * 300},
        {"schema": 2},
        {"event_file": {"path": "/tmp/x", "device": 1}},
    ],
    ids=["nonce-format", "nonce-case", "empty-agent", "huge-agent", "schema", "partial-file"],
)
def test_probe_control_rejects_a_malformed_body(probe_registry, probe_identity, overrides):
    assert _register(probe_registry, probe_identity, **overrides).status_code == 400
    assert probe_registry.live_count() == 0


def test_probe_control_rejects_an_unknown_field(probe_registry, probe_identity):
    assert _register(probe_registry, probe_identity, extra="nope").status_code == 400


def test_probe_control_rejects_a_non_object_body(probe_registry):
    handler = lambda _request: httpx.Response(200)  # noqa: E731 - unused upstream
    response = asyncio.run(
        _request(
            handler,
            "POST",
            SUBAGENT_PROBE_PATH,
            probe_registry=probe_registry,
            json=[1, 2],
            headers={"X-AGW-Key": "local-key"},
        )
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "overrides",
    [{"model": "gpt-5.6-luna"}, {"provider": "copilot"}],
    ids=["inactive-model", "wrong-provider"],
)
def test_probe_control_requires_an_exact_active_route(probe_registry, probe_identity, overrides):
    assert _register(probe_registry, probe_identity, **overrides).status_code == 409


def test_probe_control_rejects_an_event_file_outside_the_probe_directory(
    probe_registry, probe_identity, tmp_path
):
    outside = tmp_path / "elsewhere.ndjson"
    outside.touch(mode=0o600)
    info = outside.lstat()
    response = _register(
        probe_registry,
        probe_identity,
        event_file={
            "path": str(outside),
            "device": info.st_dev,
            "inode": info.st_ino,
            "uid": info.st_uid,
            "mode": 0o600,
        },
    )
    assert response.status_code == 409


def test_probe_control_rejects_a_duplicate_registration(probe_registry, probe_identity):
    assert _register(probe_registry, probe_identity).status_code == 201
    assert _register(probe_registry, probe_identity).status_code == 409


def test_probe_control_diagnostics_carry_no_key_or_path(probe_registry, probe_identity, tmp_path):
    outside = tmp_path / "elsewhere.ndjson"
    outside.touch(mode=0o600)
    info = outside.lstat()
    response = _register(
        probe_registry,
        probe_identity,
        event_file={
            "path": str(outside),
            "device": info.st_dev,
            "inode": info.st_ino,
            "uid": info.st_uid,
            "mode": 0o600,
        },
    )
    body = response.text
    assert "local-key" not in body
    assert str(outside) not in body


# --------------------------------------------------------------------------- #
# Lifecycle headers and route evidence
# --------------------------------------------------------------------------- #
LIFECYCLE_HEADERS = {
    "X-AGW-Key": "local-key",
    "x-claude-code-agent-id": AGENT_ID,
    "x-claude-code-parent-agent-id": "parent-1",
    "X-Claude-Code-Session-Id": "session-1",
}


def test_external_request_strips_claude_agent_lifecycle_headers():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"external")

    response = _send(
        handler,
        "POST",
        "/v1/messages",
        json={"model": "gpt-5.6-sol", "messages": []},
        headers=LIFECYCLE_HEADERS,
    )

    assert response.status_code == 200
    assert "x-claude-code-agent-id" not in seen[0].headers
    assert "x-claude-code-parent-agent-id" not in seen[0].headers
    # Session id remains part of Claude Code's existing protocol contract.
    assert seen[0].headers["x-claude-code-session-id"] == "session-1"


def test_native_request_keeps_its_lifecycle_headers():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"native")

    _send(
        handler,
        "POST",
        "/v1/messages",
        json={"model": "claude-sonnet-4-5", "messages": []},
        headers=LIFECYCLE_HEADERS,
    )

    assert seen[0].headers["x-claude-code-agent-id"] == AGENT_ID
    assert seen[0].headers["x-claude-code-parent-agent-id"] == "parent-1"


@pytest.mark.parametrize(
    ("url", "request_kind"),
    [("/v1/messages", "messages"), ("/v1/messages/count_tokens", "count_tokens")],
)
def test_a_registered_external_request_records_one_route_event(
    probe_registry, probe_identity, probe_dir, url, request_kind
):
    probe_registry.register(_registration(probe_identity))
    handler = lambda _request: httpx.Response(200, content=b"external")  # noqa: E731

    response = asyncio.run(
        _request(
            handler,
            "POST",
            url,
            probe_registry=probe_registry,
            json={"model": "gpt-5.6-sol", "messages": []},
            headers=LIFECYCLE_HEADERS,
        )
    )

    assert response.status_code == 200
    events = read_probe_events(probe_dir, probe_identity)
    assert len(events) == 1
    assert events[0]["request_kind"] == request_kind
    assert events[0]["agent_id"] == AGENT_ID
    assert events[0]["requested_model"] == "gpt-5.6-sol"
    assert events[0]["resolved_model"] == "gpt-5.6-sol"
    assert events[0]["provider"] == "chatgpt"


def test_an_unregistered_external_request_writes_no_evidence(
    probe_registry, probe_identity, probe_dir
):
    handler = lambda _request: httpx.Response(200, content=b"external")  # noqa: E731
    asyncio.run(
        _request(
            handler,
            "POST",
            "/v1/messages",
            probe_registry=probe_registry,
            json={"model": "gpt-5.6-sol", "messages": []},
            headers=LIFECYCLE_HEADERS,
        )
    )
    assert read_probe_events(probe_dir, probe_identity) == ()


def test_a_native_request_never_records_route_evidence(
    probe_registry, probe_identity, probe_dir
):
    probe_registry.register(_registration(probe_identity))
    handler = lambda _request: httpx.Response(200, content=b"native")  # noqa: E731
    asyncio.run(
        _request(
            handler,
            "POST",
            "/v1/messages",
            probe_registry=probe_registry,
            json={"model": "claude-sonnet-4-5", "messages": []},
            headers=LIFECYCLE_HEADERS,
        )
    )
    assert read_probe_events(probe_dir, probe_identity) == ()
    assert probe_registry.live_count() == 1


def test_ordinary_sessions_have_no_registry_and_persist_nothing(probe_dir, probe_identity):
    handler = lambda _request: httpx.Response(200, content=b"external")  # noqa: E731
    response = _send(
        handler,
        "POST",
        "/v1/messages",
        json={"model": "gpt-5.6-sol", "messages": []},
        headers=LIFECYCLE_HEADERS,
    )
    assert response.status_code == 200
    assert read_probe_events(probe_dir, probe_identity) == ()
