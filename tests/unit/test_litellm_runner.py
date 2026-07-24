"""Compatibility tests for AGW's pinned LiteLLM entry point."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

from agent_gateway.litellm_runner import (
    _run_server,
    _wrap_anthropic_response_translator,
    _wrap_aresponses,
    _wrap_reasoning_effort_normalizer,
    _wrap_responses_tool_choice_translator,
    _wrap_stream_event_processor,
    enable_chatgpt_responses_bridge,
)


def test_chatgpt_is_added_to_anthropic_responses_dispatch() -> None:
    from litellm.llms.anthropic.experimental_pass_through.messages import handler

    original = handler._RESPONSES_API_PROVIDERS
    try:
        enable_chatgpt_responses_bridge()
        assert handler._should_route_to_responses_api("chatgpt") is True
        assert handler._should_route_to_responses_api("openai") is True
    finally:
        handler._RESPONSES_API_PROVIDERS = original


def test_existing_responses_adapter_maps_system_to_instructions() -> None:
    from litellm.llms.anthropic.experimental_pass_through.responses_adapters.transformation import (
        LiteLLMAnthropicToResponsesAPIAdapter,
    )

    translated = LiteLLMAnthropicToResponsesAPIAdapter().translate_request(
        {
            "model": "gpt-5.6-sol",
            "max_tokens": 16,
            "system": "Claude Code system prompt",
            "messages": [{"role": "user", "content": "hello"}],
        }
    )

    assert translated["instructions"] == "Claude Code system prompt"
    assert all(item.get("role") != "system" for item in translated["input"])


def test_client_web_search_stays_a_function_tool_for_chatgpt() -> None:
    from litellm.llms.anthropic.experimental_pass_through.responses_adapters.transformation import (
        LiteLLMAnthropicToResponsesAPIAdapter,
    )

    translated = LiteLLMAnthropicToResponsesAPIAdapter().translate_request(
        {
            "model": "gpt-5.6-sol",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "search"}],
            "tools": [
                {
                    "name": "WebSearch",
                    "description": "Search the web",
                    "input_schema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                }
            ],
        }
    )

    assert translated["tools"] == [
        {
            "type": "function",
            "name": "WebSearch",
            "description": "Search the web",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        }
    ]


def test_responses_tool_choice_uses_openai_scalar_values() -> None:
    from litellm.llms.anthropic.experimental_pass_through.responses_adapters.transformation import (
        LiteLLMAnthropicToResponsesAPIAdapter,
    )

    translate = _wrap_responses_tool_choice_translator(
        LiteLLMAnthropicToResponsesAPIAdapter.translate_tool_choice_to_responses_api
    )

    assert translate({"type": "auto"}) == "auto"
    assert translate({"type": "any"}) == "required"
    assert translate({"type": "tool", "name": "WebSearch"}) == {
        "type": "function",
        "name": "WebSearch",
    }


def test_streaming_response_suppresses_reasoning_and_reindexes_text() -> None:
    from litellm.llms.anthropic.experimental_pass_through.responses_adapters import (
        streaming_iterator,
    )

    wrapper = streaming_iterator.AnthropicResponsesStreamWrapper(
        responses_stream=None,
        model="gpt-5.6-sol",
    )
    process = _wrap_stream_event_processor(
        streaming_iterator.AnthropicResponsesStreamWrapper._process_event
    )
    process(
        wrapper,
        {
            "type": "response.output_item.added",
            "item": {"type": "reasoning", "id": "reasoning-1"},
        },
    )
    process(
        wrapper,
        {
            "type": "response.reasoning_summary_text.delta",
            "item_id": "reasoning-1",
            "delta": "private summary",
        },
    )
    process(
        wrapper,
        {
            "type": "response.output_item.done",
            "item": {"type": "reasoning", "id": "reasoning-1"},
        },
    )
    process(
        wrapper,
        {
            "type": "response.output_item.added",
            "item": {"type": "message", "id": "message-1"},
        },
    )
    process(
        wrapper,
        {
            "type": "response.output_text.delta",
            "item_id": "message-1",
            "delta": "answer",
        },
    )

    chunks = list(wrapper._chunk_queue)
    assert chunks == [
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "answer"},
        },
    ]


def test_streaming_response_keeps_tool_use_after_suppressed_reasoning() -> None:
    from litellm.llms.anthropic.experimental_pass_through.responses_adapters import (
        streaming_iterator,
    )

    wrapper = streaming_iterator.AnthropicResponsesStreamWrapper(
        responses_stream=None,
        model="gpt-5.6-sol",
    )
    process = _wrap_stream_event_processor(
        streaming_iterator.AnthropicResponsesStreamWrapper._process_event
    )
    process(
        wrapper,
        {
            "type": "response.output_item.added",
            "item": {"type": "reasoning", "id": "reasoning-1"},
        },
    )
    process(
        wrapper,
        {
            "type": "response.output_item.done",
            "item": {"type": "reasoning", "id": "reasoning-1"},
        },
    )
    process(
        wrapper,
        {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "call-item-1",
                "call_id": "call-1",
                "name": "Read",
            },
        },
    )

    chunks = list(wrapper._chunk_queue)
    assert chunks == [
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": "call-1",
                "name": "Read",
                "input": {},
            },
        }
    ]


def test_nonstream_response_suppresses_unsigned_reasoning_summary() -> None:
    translated = SimpleNamespace(
        content=[
            {"type": "thinking", "thinking": "private summary", "signature": None},
            {"type": "text", "text": "answer"},
        ]
    )

    def original(_adapter: Any, _response: Any) -> Any:
        return translated

    wrapped = _wrap_anthropic_response_translator(original)
    result = wrapped(object(), object())

    assert result is translated
    assert result.content == [{"type": "text", "text": "answer"}]


def test_claude_effort_reaches_chatgpt_unchanged() -> None:
    from litellm.llms.anthropic.experimental_pass_through import utils
    from litellm.llms.anthropic.experimental_pass_through.responses_adapters.handler import (
        _build_responses_kwargs,
    )

    original = utils.normalize_reasoning_effort_value
    utils_module: Any = utils
    utils_module.normalize_reasoning_effort_value = _wrap_reasoning_effort_normalizer(original)
    try:
        for effort in ("low", "medium", "high", "xhigh", "max"):
            translated = _build_responses_kwargs(
                max_tokens=16,
                messages=[{"role": "user", "content": "hello"}],
                model="gpt-5.6-sol",
                thinking={"type": "adaptive"},
                output_config={"effort": effort},
                extra_kwargs={"custom_llm_provider": "chatgpt"},
            )
            assert translated["reasoning"]["effort"] == effort
    finally:
        utils_module.normalize_reasoning_effort_value = original


def test_effort_bridge_does_not_change_other_providers() -> None:
    calls: list[tuple[str, str, str | None]] = []

    def original(effort: str, model: str, provider: str | None = None) -> str:
        calls.append((effort, model, provider))
        return "normalized"

    wrapped = _wrap_reasoning_effort_normalizer(original)

    assert wrapped("max", "other-model", "openai") == "normalized"
    assert calls == [("max", "other-model", "openai")]


def test_effort_bridge_keeps_normalization_for_older_chatgpt_models() -> None:
    calls: list[tuple[str, str, str | None]] = []

    def original(effort: str, model: str, provider: str | None = None) -> str:
        calls.append((effort, model, provider))
        return "normalized"

    wrapped = _wrap_reasoning_effort_normalizer(original)

    assert wrapped("max", "gpt-4.1", "chatgpt") == "normalized"
    assert wrapped("none", "gpt-5.6-sol", "chatgpt") == "normalized"
    assert calls == [
        ("max", "gpt-4.1", "chatgpt"),
        ("none", "gpt-5.6-sol", "chatgpt"),
    ]


def test_chatgpt_nonstream_drains_always_sse_response() -> None:
    completed = object()

    class AlwaysSSE:
        def __aiter__(self) -> AsyncIterator[Any]:
            return self.events()

        async def events(self) -> AsyncIterator[Any]:
            yield SimpleNamespace(type="response.output_text.delta", delta="ok")
            yield SimpleNamespace(type="response.completed", response=completed)

    async def original(**_kwargs: Any) -> Any:
        return AlwaysSSE()

    wrapped = _wrap_aresponses(original)

    async def exercise() -> Any:
        return await wrapped(custom_llm_provider="chatgpt", stream=False)

    result: Any = asyncio.run(exercise())

    assert result is completed


def test_nonstream_coercion_does_not_depend_on_forwarded_provider() -> None:
    completed = object()

    class AlwaysSSE:
        def __aiter__(self) -> AsyncIterator[Any]:
            return self.events()

        async def events(self) -> AsyncIterator[Any]:
            yield SimpleNamespace(type="response.completed", response=completed)

    async def original(**_kwargs: Any) -> Any:
        return AlwaysSSE()

    wrapped = _wrap_aresponses(original)

    async def exercise() -> Any:
        return await wrapped(stream=False)

    assert asyncio.run(exercise()) is completed


def test_nonstream_rehydrates_output_items_from_sse() -> None:
    from litellm.types.llms.openai import ResponsesAPIResponse
    from openai.types.responses import ResponseOutputMessage

    completed = ResponsesAPIResponse(id="resp_test", created_at=0, output=[])
    item = {
        "type": "message",
        "id": "msg_test",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "ok", "annotations": []}],
    }

    class Event:
        def __init__(self, event_type: str, data: dict[str, Any]) -> None:
            self.type = event_type
            self.response = data.get("response")
            self._data = {"type": event_type, **data}

        def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
            return self._data

    class AlwaysSSE:
        def __aiter__(self) -> AsyncIterator[Any]:
            return self.events()

        async def events(self) -> AsyncIterator[Any]:
            yield Event("response.output_item.done", {"output_index": 0, "item": item})
            yield Event("response.completed", {"response": completed})

    async def original(**_kwargs: Any) -> Any:
        return AlwaysSSE()

    wrapped = _wrap_aresponses(original)

    async def exercise() -> Any:
        return await wrapped(stream=False)

    result = asyncio.run(exercise())
    assert len(result.output) == 1
    assert isinstance(result.output[0], ResponseOutputMessage)


def test_streaming_chatgpt_response_is_not_drained() -> None:
    stream = object()

    async def original(**_kwargs: Any) -> Any:
        return stream

    wrapped = _wrap_aresponses(original)

    async def exercise() -> Any:
        return await wrapped(custom_llm_provider="chatgpt", stream=True)

    assert asyncio.run(exercise()) is stream


def test_runner_delegates_and_preserves_exit_code() -> None:
    assert _run_server(lambda: 17) == 17
