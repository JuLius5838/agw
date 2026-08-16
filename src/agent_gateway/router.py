"""Hybrid front router for Claude Code.

The router is the single loopback endpoint seen by Claude Code. Native Claude
model requests are streamed directly to Anthropic with the request's original
Claude Code credential. Exact configured external model names (and their hidden
picker ids) are streamed to the private LiteLLM child instead. Claude credentials
are never forwarded to LiteLLM, and provider credentials never enter this process.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import secrets
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from agent_gateway.errors import ProxyError
from agent_gateway.model_registry import ModelRegistry, load_registry_text
from agent_gateway.paths import read_text
from agent_gateway.probe_events import ProbeFileIdentity, append_probe_event, validate_probe_file
from agent_gateway.providers import Provider

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
# Stripped before any external forwarding. The two secret headers carry Claude's
# own credential; the two lifecycle ids identify a Claude Code subagent and its
# parent, which are local correlation handles that no provider needs.
# `x-claude-code-session-id` is deliberately absent: it remains part of Claude
# Code's existing protocol contract and changing it needs its own design.
_EXTERNAL_SECRET_HEADERS: Final = {"authorization", "x-api-key"}
_EXTERNAL_LOCAL_HEADERS: Final = _EXTERNAL_SECRET_HEADERS | {
    "x-claude-code-agent-id",
    "x-claude-code-parent-agent-id",
}
_AGENT_ID_HEADER: Final = "x-claude-code-agent-id"

SUBAGENT_PROBE_PATH: Final[str] = "/_agw/control/v1/subagent-probes"
SUBAGENT_PROBE_TTL_SECONDS: Final[float] = 60.0
_MAX_PROBE_REGISTRATIONS: Final[int] = 32
_MAX_AGENT_ID_LENGTH: Final[int] = 200
_NONCE_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}")
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
class ProbeRegistration:
    """One authorization to record a single route event for one subagent."""

    agent_id: str
    model: str
    provider: Provider
    nonce: str
    file: ProbeFileIdentity
    expires_at: float


class OneShotProbeRegistry:
    """In-memory authorizations for the explicit compatibility probe only.

    Ordinary sessions never register anything, so the router keeps its usual
    property of persisting no per-request metadata. A registration exists only
    between a ``SubagentStart`` hook and that subagent's first model request, is
    consumed by exactly one matching request, and expires after a minute.

    Deliberately not persisted: a router restart loses every registration and the
    probe then fails closed, which is the correct outcome — evidence that
    survived a restart would no longer describe the process that produced it.
    """

    def __init__(
        self,
        probe_dir: Path,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.probe_dir = probe_dir
        self._clock = clock
        self._lock = threading.Lock()
        self._registrations: dict[str, ProbeRegistration] = {}

    def _drop_expired(self) -> None:
        now = self._clock()
        expired = [key for key, item in self._registrations.items() if item.expires_at <= now]
        for key in expired:
            del self._registrations[key]

    def now(self) -> float:
        """The registry's monotonic clock, so callers compute expiry the same way."""
        return self._clock()

    def live_count(self) -> int:
        with self._lock:
            self._drop_expired()
            return len(self._registrations)

    def register(self, registration: ProbeRegistration) -> None:
        with self._lock:
            self._drop_expired()
            if registration.agent_id in self._registrations:
                raise ValueError("agent id is already registered")
            if any(item.nonce == registration.nonce for item in self._registrations.values()):
                raise ValueError("nonce is already registered")
            if len(self._registrations) >= _MAX_PROBE_REGISTRATIONS:
                raise ProxyError("too many active subagent probe registrations.")
            self._registrations[registration.agent_id] = registration

    def consume(
        self,
        *,
        agent_id: str,
        requested_model: str,
        resolved_model: str,
        provider: Provider,
        request_kind: str,
        timestamp: str,
    ) -> bool:
        """Record one route event iff every field matches exactly.

        A near miss returns ``False`` without consuming anything: the real
        request must still be able to satisfy the registration. Only a successful
        append retires it.
        """
        with self._lock:
            self._drop_expired()
            registration = self._registrations.get(agent_id)
            if registration is None:
                return False
            if (
                requested_model != registration.model
                or resolved_model != registration.model
                or provider is not registration.provider
            ):
                return False

            event = {
                "schema": 1,
                "event": "route",
                "nonce": registration.nonce,
                "agent_id": registration.agent_id,
                "requested_model": requested_model,
                "resolved_model": resolved_model,
                "provider": provider.value,
                "request_kind": request_kind,
                "timestamp": timestamp,
            }
            try:
                append_probe_event(self.probe_dir, registration.file, event)
            except ProxyError:
                # Losing the write means the evidence chain is broken; drop the
                # registration so nothing later appears to satisfy it.
                del self._registrations[agent_id]
                raise
            del self._registrations[agent_id]
            return True


@dataclass(frozen=True)
class RouterSettings:
    """Runtime settings, injectable in tests without process environment changes."""

    registry: ModelRegistry
    local_key: str
    internal_key: str
    litellm_url: str | None
    anthropic_url: str = DEFAULT_ANTHROPIC_BASE_URL
    transport: httpx.AsyncBaseTransport | None = None
    # Present only while an explicit compatibility probe is running.
    probe_registry: OneShotProbeRegistry | None = None


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
        if external and lowered in _EXTERNAL_LOCAL_HEADERS:
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
        native_search = isinstance(tool_type, str) and tool_type.startswith("web_search_")
        if native_search or tool_name == "web_search":
            converted.append(_CLIENT_WEB_SEARCH_TOOL)
        else:
            converted.append(tool)
    payload["tools"] = converted


def _strip_invalid_unsigned_thinking(payload: dict[str, object]) -> bool:
    """Remove legacy GPT reasoning placeholders before a native Claude request.

    Claude's omitted-thinking mode legitimately returns an empty ``thinking``
    field with a non-empty Anthropic signature. Preserve those blocks, all
    readable thinking, and redacted thinking. Only the empty, unsigned blocks
    produced by the ChatGPT Responses compatibility adapter are invalid.
    """
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return False

    changed = False
    cleaned_messages: list[object] = []
    for message in messages:
        if not isinstance(message, dict):
            cleaned_messages.append(message)
            continue
        content = message.get("content")
        if not isinstance(content, list):
            cleaned_messages.append(message)
            continue

        cleaned_content: list[object] = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "thinking":
                cleaned_content.append(block)
                continue
            thinking = block.get("thinking")
            signature = block.get("signature")
            has_thinking = isinstance(thinking, str) and bool(thinking)
            has_signature = isinstance(signature, str) and bool(signature)
            if has_thinking or has_signature:
                cleaned_content.append(block)
            else:
                changed = True

        if message.get("role") == "assistant" and content and not cleaned_content:
            changed = True
            continue
        if len(cleaned_content) != len(content):
            cleaned_message = dict(message)
            cleaned_message["content"] = cleaned_content
            cleaned_messages.append(cleaned_message)
        else:
            cleaned_messages.append(message)

    if changed:
        payload["messages"] = cleaned_messages
    return changed


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


def _is_loopback_peer(request: Request) -> bool:
    client = request.scope.get("client")
    if not client:
        return False
    try:
        return ipaddress.ip_address(str(client[0])).is_loopback
    except ValueError:
        return False


def _probe_error(status: int, message: str) -> Response:
    """Control-plane errors name the condition only, never a value."""
    return JSONResponse({"error": {"message": message}}, status_code=status)


def _parse_registration(
    payload: object,
    settings: RouterSettings,
) -> ProbeRegistration | Response:
    """Validate a registration body strictly; every field must be exact."""
    if not isinstance(payload, dict):
        return _probe_error(400, "body must be a JSON object")
    allowed = {"schema", "nonce", "agent_id", "model", "provider", "event_file"}
    if set(payload) != allowed:
        return _probe_error(400, "body must contain exactly the registration fields")
    if payload.get("schema") != 1:
        return _probe_error(400, "unsupported registration schema")

    nonce = payload.get("nonce")
    if not isinstance(nonce, str) or not _NONCE_PATTERN.fullmatch(nonce):
        return _probe_error(400, "nonce must be 32 lowercase hex characters")

    agent_id = payload.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id or len(agent_id) > _MAX_AGENT_ID_LENGTH:
        return _probe_error(400, "agent_id must be a short non-empty string")

    model = payload.get("model")
    provider_name = payload.get("provider")
    if not isinstance(model, str) or not isinstance(provider_name, str):
        return _probe_error(400, "model and provider must be strings")

    event_file = payload.get("event_file")
    if not isinstance(event_file, dict) or set(event_file) != {
        "path",
        "device",
        "inode",
        "uid",
        "mode",
    }:
        return _probe_error(400, "event_file must contain exactly the file identity fields")
    if not isinstance(event_file.get("path"), str):
        return _probe_error(400, "event_file.path must be a string")
    for numeric in ("device", "inode", "uid", "mode"):
        if not isinstance(event_file.get(numeric), int) or isinstance(event_file[numeric], bool):
            return _probe_error(400, "event_file identity fields must be integers")

    # A probe may only ever be registered for an exact active route. Anything
    # else would let a caller record evidence for a model AGW does not serve.
    try:
        provider = Provider(provider_name)
    except ValueError:
        return _probe_error(409, "provider is not an active route")
    entry = next(
        (item for item in settings.registry.active_models() if item.name == model),
        None,
    )
    if entry is None or entry.provider is not provider:
        return _probe_error(409, "model is not an active route for that provider")

    identity = ProbeFileIdentity(
        path=str(event_file["path"]),
        device=int(event_file["device"]),
        inode=int(event_file["inode"]),
        uid=int(event_file["uid"]),
        mode=int(event_file["mode"]),
    )
    assert settings.probe_registry is not None  # checked by the caller
    try:
        validate_probe_file(settings.probe_registry.probe_dir, identity)
    except ProxyError:
        return _probe_error(409, "event file failed safety validation")

    return ProbeRegistration(
        agent_id=agent_id,
        model=model,
        provider=provider,
        nonce=nonce,
        file=identity,
        expires_at=0.0,  # replaced by the endpoint; callers never choose expiry
    )


async def _subagent_probes(request: Request) -> Response:
    """Authorize one route recording for one subagent, from loopback only."""
    settings: RouterSettings = request.app.state.settings
    if not _authorized(request, settings.local_key):
        return _probe_error(401, "unauthorized")
    if not _is_loopback_peer(request):
        return _probe_error(403, "forbidden")
    if settings.probe_registry is None:
        return _probe_error(503, "subagent probes are not enabled")

    try:
        payload = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _probe_error(400, "body must be a JSON object")

    parsed = _parse_registration(payload, settings)
    if isinstance(parsed, Response):
        return parsed

    registry = settings.probe_registry
    expires_at = registry.now() + SUBAGENT_PROBE_TTL_SECONDS
    try:
        registry.register(
            ProbeRegistration(
                agent_id=parsed.agent_id,
                model=parsed.model,
                provider=parsed.provider,
                nonce=parsed.nonce,
                file=parsed.file,
                expires_at=expires_at,
            )
        )
    except ValueError:
        return _probe_error(409, "already registered")
    except ProxyError:
        return _probe_error(429, "too many active registrations")

    return JSONResponse(
        {"ok": True, "expires_in": int(SUBAGENT_PROBE_TTL_SECONDS)},
        status_code=201,
    )


def _record_probe_route(
    request: Request,
    settings: RouterSettings,
    *,
    requested_model: str,
    resolved_model: str,
    provider: Provider,
) -> None:
    """Record exact route evidence while a controlled probe is registered.

    Called before streaming upstream, so the local route is proven even if the
    external provider subsequently fails. The verifier separately requires the
    child to complete, so this alone cannot make a probe pass.
    """
    registry = settings.probe_registry
    if registry is None:
        return
    agent_id = request.headers.get(_AGENT_ID_HEADER, "")
    if not agent_id:
        return
    request_kind = "count_tokens" if request.url.path.endswith("/count_tokens") else "messages"
    registry.consume(
        agent_id=agent_id,
        requested_model=requested_model,
        resolved_model=resolved_model,
        provider=provider,
        request_kind=request_kind,
        timestamp=datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


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
        native_body = body
        if isinstance(payload, dict) and _strip_invalid_unsigned_thinking(payload):
            native_body = json.dumps(
                payload,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        return await _stream_upstream(
            request,
            settings,
            target_base=settings.anthropic_url,
            body=native_body,
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
    requested_model = model
    payload["model"] = external_entry.name
    if external_entry.provider.value == "chatgpt":
        _use_client_side_web_search(payload)
    _record_probe_route(
        request,
        settings,
        requested_model=requested_model,
        resolved_model=external_entry.name,
        provider=external_entry.provider,
    )
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
            # Declared before the message routes so the control plane can never
            # be shadowed by a future catch-all.
            Route(SUBAGENT_PROBE_PATH, _subagent_probes, methods=["POST"]),
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
    parser.add_argument("--probe-dir", required=True)
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
            probe_registry=OneShotProbeRegistry(Path(args.probe_dir)),
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
