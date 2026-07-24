"""Unit tests for model listing and the compatibility-check logic.

The verification checks accept an injectable ``httpx.Client``, so we drive them
with ``httpx.MockTransport`` — no real proxy, network, or upstream involved.
"""

from __future__ import annotations

import json

import httpx

from agent_gateway.model_registry import load_registry_text
from agent_gateway.models import (
    CheckStatus,
    _count_tokens,
    _non_streaming,
    _streaming,
    _tool_use,
    list_models,
    list_models_json,
    render_table,
)

REGISTRY = load_registry_text(
    """
    default_model: gpt-5.3-codex
    models:
      - name: gpt-5.3-codex
        provider: chatgpt
        upstream_model: chatgpt/gpt-5.3-codex
        mode: responses
        enabled: true
      - name: copilot-gpt
        provider: copilot
        upstream_model: github_copilot/copilot-gpt
        mode: chat
        enabled: false
    """
)

BASE = "http://127.0.0.1:4000"
KEY = "sk-agw-test"


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #
def test_list_active_only_by_default():
    items = list_models(REGISTRY)
    assert [i.name for i in items] == ["gpt-5.3-codex"]
    assert items[0].is_default is True


def test_list_all_includes_inactive():
    names = {i.name for i in list_models(REGISTRY, include_inactive=True)}
    assert names == {"gpt-5.3-codex", "copilot-gpt"}


def test_json_has_stable_schema_and_no_secrets():
    doc = json.loads(list_models_json(REGISTRY, include_inactive=True))
    assert doc["default_model"] == "gpt-5.3-codex"
    assert {"name", "provider", "mode", "enabled", "is_default"} == set(doc["models"][0])
    assert "token" not in json.dumps(doc).lower()
    assert "/" not in doc["models"][0]["name"]  # no provider prefix leaks


def test_render_table_marks_default():
    table = render_table(list_models(REGISTRY, include_inactive=True))
    assert "gpt-5.3-codex" in table
    assert "*" in table  # default marker


# --------------------------------------------------------------------------- #
# Verification checks (MockTransport)
# --------------------------------------------------------------------------- #
def _client(handler) -> httpx.Client:  # noqa: ANN001
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_count_tokens_pass_and_fail():
    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"input_tokens": 3})

    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "nope"})

    assert _count_tokens(_client(ok), BASE, KEY, "m").status is CheckStatus.passed
    assert _count_tokens(_client(bad), BASE, KEY, "m").status is CheckStatus.failed


def test_non_streaming_requires_message_shape():
    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"type": "message", "content": [{"type": "text", "text": "ok"}]}
        )

    def wrong(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"type": "error"})

    assert _non_streaming(_client(ok), BASE, KEY, "m").status is CheckStatus.passed
    assert _non_streaming(_client(wrong), BASE, KEY, "m").status is CheckStatus.failed


def test_streaming_requires_start_and_stop():
    good_sse = (
        b'event: message_start\ndata: {"type":"message_start"}\n\n'
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    )
    partial_sse = b'event: message_start\ndata: {"type":"message_start"}\n\n'

    assert (
        _streaming(_client(lambda r: httpx.Response(200, content=good_sse)), BASE, KEY, "m").status
        is CheckStatus.passed
    )
    assert (
        _streaming(
            _client(lambda r: httpx.Response(200, content=partial_sse)), BASE, KEY, "m"
        ).status
        is CheckStatus.failed
    )


def test_tool_use_requires_tool_block():
    def with_tool(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [{"type": "tool_use", "name": "get_weather", "input": {"city": "Paris"}}]
            },
        )

    def text_only(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": [{"type": "text", "text": "it is sunny"}]})

    assert _tool_use(_client(with_tool), BASE, KEY, "m").status is CheckStatus.passed
    assert _tool_use(_client(text_only), BASE, KEY, "m").status is CheckStatus.failed


def test_checks_send_required_anthropic_headers():
    seen: dict[str, str] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"input_tokens": 1})

    _count_tokens(_client(capture), BASE, KEY, "m")
    assert seen.get("anthropic-version") == "2023-06-01"
    assert seen.get("x-agw-key") == KEY
