"""Unit tests for the hybrid front router's routing and credential boundary."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx

from agent_gateway.model_registry import load_registry_text
from agent_gateway.router import RouterSettings, create_app

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
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
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
