"""Hybrid front router for Claude Code.

The router is the single loopback endpoint seen by Claude Code. Native Claude
model requests are streamed directly to Anthropic with the request's original
Claude Code credential. Exact configured external model names (and their hidden
picker ids) are streamed to the private LiteLLM child instead. Claude credentials
are never forwarded to LiteLLM, and provider credentials never enter this process.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from agent_gateway.model_registry import ModelRegistry, load_registry_text
from agent_gateway.paths import read_text

DEFAULT_ANTHROPIC_BASE_URL: Final = "https://api.anthropic.com"
_LOCAL_KEY_HEADER: Final = "x-agw-key"
_HOP_BY_HOP: Final = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}
_EXTERNAL_SECRET_HEADERS: Final = {"authorization", "x-api-key"}
_CLIENT_WEB_SEARCH_TOOL: Final = {
    "name": "WebSearch",
    "description": (
        "Search the web for up-to-date information. The Claude Code client "
        "executes this tool and returns the results."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query."},
            "allowed_domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional domains to include.",
            },
            "blocked_domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional domains to exclude.",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}


@dataclass(frozen=True)
class RouterSettings:
    """Runtime settings, injectable in tests without process environment changes."""

    registry: ModelRegistry
    local_key: str
    internal_key: str
    litellm_url: str | None
    anthropic_url: str = DEFAULT_ANTHROPIC_BASE_URL
    transport: httpx.AsyncBaseTransport | None = None


def _authorized(request: Request, expected: str) -> bool:
    presented = request.headers.get(_LOCAL_KEY_HEADER, "")
    return bool(presented) and secrets.compare_digest(presented, expected)


def _target_url(base: str, request: Request) -> str:
    query = request.scope.get("query_string", b"")
    suffix = request.url.path
    if query:
        suffix += "?" + bytes(query).decode("ascii")
    return base.rstrip("/") + suffix


def _request_headers(request: Request, *, external: bool, internal_key: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name, value in request.headers.items():
        lowered = name.lower()
        if lowered in _HOP_BY_HOP or lowered == _LOCAL_KEY_HEADER:
            continue
        if external and lowered in _EXTERNAL_SECRET_HEADERS:
            continue
        headers[name] = value
    if external:
        headers["Authorization"] = f"Bearer {internal_key}"
    return headers


def _response_headers(response: httpx.Response) -> dict[str, str]:
    return {
        name: value for name, value in response.headers.items() if name.lower() not in _HOP_BY_HOP
    }


def _use_client_side_web_search(payload: dict[str, object]) -> None:
    """Convert Anthropic hosted search into Claude Code's local WebSearch tool.

    The ChatGPT subscription endpoint rejects OpenAI's hosted
    ``web_search_preview`` tool. A fully formed ``WebSearch`` function remains
    client-side: Codex requests it, then Claude Code performs the search and
    returns an ordinary tool result.
    """
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return
    converted: list[object] = []
    for tool in tools:
        if not isinstance(tool, dict):
            converted.append(tool)
            continue
        tool_type = tool.get("type")
        tool_name = tool.get("name")
        native_search = (
            isinstance(tool_type, str) and tool_type.startswith("web_search_")
        )
        if native_search or tool_name == "web_search":
            converted.append(_CLIENT_WEB_SEARCH_TOOL)
        else:
            converted.append(tool)
    payload["tools"] = converted


async def _stream_upstream(
    request: Request,
    settings: RouterSettings,
    *,
    target_base: str,
    body: bytes,
    external: bool,
) -> Response:
    client = httpx.AsyncClient(
        transport=settings.transport,
        follow_redirects=False,
        timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0),
    )
    upstream_request = client.build_request(
        request.method,
        _target_url(target_base, request),
        headers=_request_headers(request, external=external, internal_key=settings.internal_key),
        content=body,
    )
    try:
        upstream = await client.send(upstream_request, stream=True)
    except httpx.HTTPError:
        await client.aclose()
        return JSONResponse(
            {
                "type": "error",
                "error": {"type": "upstream_unavailable", "message": "upstream unavailable"},
            },
            status_code=502,
        )

    async def stream() -> AsyncIterator[bytes]:
        try:
            if upstream.is_stream_consumed:
                yield upstream.content
            else:
                async for chunk in upstream.aiter_raw():
                    yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        stream(),
        status_code=upstream.status_code,
        headers=_response_headers(upstream),
    )


async def _root(_request: Request) -> Response:
    return Response(status_code=200)


async def _litellm_ready(settings: RouterSettings) -> bool:
    """Report whether the private LiteLLM application is ready, not just listening."""
    if settings.litellm_url is None:
        return True
    try:
        async with httpx.AsyncClient(
            transport=settings.transport,
            timeout=httpx.Timeout(2.0),
        ) as client:
            response = await client.get(
                settings.litellm_url.rstrip("/") + "/health/readiness",
                headers={"Authorization": f"Bearer {settings.internal_key}"},
            )
    except httpx.HTTPError:
        return False
    return response.status_code == 200


async def _models(request: Request) -> Response:
    settings: RouterSettings = request.app.state.settings
    if not _authorized(request, settings.local_key):
        return JSONResponse({"error": {"message": "unauthorized"}}, status_code=401)
    if not await _litellm_ready(settings):
        return JSONResponse(
            {"error": {"message": "external model backend is starting"}},
            status_code=503,
        )

    entries = [
        {
            "id": picker_id,
            "display_name": display_name,
            "type": "model",
            "object": "model",
            "owned_by": "agent-gateway",
        }
        for picker_id, display_name in settings.registry.picker_models()
    ]
    return JSONResponse({"object": "list", "data": entries, "has_more": False})


async def _messages(request: Request) -> Response:
    settings: RouterSettings = request.app.state.settings
    if not _authorized(request, settings.local_key):
        return JSONResponse(
            {"type": "error", "error": {"type": "authentication_error", "message": "unauthorized"}},
            status_code=401,
        )

    body = await request.body()
    try:
        payload = json.loads(body)
        model = payload.get("model") if isinstance(payload, dict) else None
    except (json.JSONDecodeError, UnicodeDecodeError):
        model = None
        payload = None
    if not isinstance(model, str) or not model:
        return JSONResponse(
            {
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "request must contain a model",
                },
            },
            status_code=400,
        )

    external_entry = settings.registry.resolve_routed_model(model)
    if external_entry is None:
        return await _stream_upstream(
            request,
            settings,
            target_base=settings.anthropic_url,
            body=body,
            external=False,
        )

    if settings.litellm_url is None:
        return JSONResponse(
            {
                "type": "error",
                "error": {
                    "type": "model_unavailable",
                    "message": f"external model '{external_entry.name}' is not available",
                },
            },
            status_code=503,
        )

    assert isinstance(payload, dict)  # established by the string model above
    payload["model"] = external_entry.name
    if external_entry.provider.value == "chatgpt":
        _use_client_side_web_search(payload)
    external_body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    return await _stream_upstream(
        request,
        settings,
        target_base=settings.litellm_url,
        body=external_body,
        external=True,
    )


def create_app(settings: RouterSettings) -> Starlette:
    """Create the ASGI app. No request body or credential is ever logged."""
    app = Starlette(
        debug=False,
        routes=[
            Route("/", _root, methods=["HEAD"]),
            Route("/v1/models", _models, methods=["GET"]),
            Route("/v1/messages", _messages, methods=["POST"]),
            Route("/v1/messages/count_tokens", _messages, methods=["POST"]),
        ],
    )
    app.state.settings = settings
    return app


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="agent_gateway.router")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--models-file", required=True)
    parser.add_argument("--litellm-url")
    parser.add_argument("--anthropic-url", default=DEFAULT_ANTHROPIC_BASE_URL)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    local_key = os.environ.get("AGW_LOCAL_KEY")
    internal_key = os.environ.get("AGW_LITELLM_KEY")
    if not local_key or not internal_key:
        print("router keys are missing", file=sys.stderr)
        return 2
    registry = load_registry_text(read_text(Path(args.models_file)))
    app = create_app(
        RouterSettings(
            registry=registry,
            local_key=local_key,
            internal_key=internal_key,
            litellm_url=args.litellm_url,
            anthropic_url=args.anthropic_url,
        )
    )
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        access_log=False,
        log_level="warning",
        server_header=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
