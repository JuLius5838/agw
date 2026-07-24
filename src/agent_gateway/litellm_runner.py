"""Pinned LiteLLM entry point with the ChatGPT Responses compatibility shim.

LiteLLM 1.93.0 already contains the complete Anthropic Messages ↔ OpenAI
Responses adapter, but its dispatch allowlist applies that adapter only to the
``openai`` provider. The subscription-backed ``chatgpt`` provider therefore
falls through to the chat-completions adapter while still posting to ChatGPT's
``/codex/responses`` endpoint. That malformed combination turns Claude Code's
system prompt into a ``system`` input item, which the endpoint rejects.

The ChatGPT backend also returns SSE even when the caller asks for a non-streaming
response. LiteLLM exposes that as a Responses iterator, while its Anthropic
adapter expects a completed response object. AGW drains that provider-specific
iterator and returns its final response before the adapter translates it.

Finally, LiteLLM's capability lookup does not recognize ChatGPT subscription
slugs such as ``gpt-5.6-sol`` and downgrades ``xhigh``/``max`` to ``high``.
AGW preserves GPT-5.6's supported effort values for that provider so Claude
Code's native ``/effort`` selector reaches Codex unchanged.

The pinned Anthropic→Responses adapter also serializes ``auto`` and ``required``
tool choices as objects, while the Responses API requires scalar strings. AGW
normalizes those two values so Claude Code's client-side WebSearch tool can run.

AGW pins LiteLLM exactly, so these compatibility fixes are deliberately narrow.
Tests fail loudly if the pinned internal surfaces change during an upgrade.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

_WRAPPER_MARKER = "__agw_chatgpt_nonstream_bridge__"
_EFFORT_WRAPPER_MARKER = "__agw_chatgpt_effort_bridge__"
_TOOL_CHOICE_WRAPPER_MARKER = "__agw_responses_tool_choice_bridge__"
_GPT56_REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})


def _is_gpt56_slug(model: str) -> bool:
    """Return whether a private/public slug belongs to the GPT-5.6 family."""
    return model == "gpt-5.6" or model.startswith("gpt-5.6-")


async def _completed_chatgpt_response(result: Any) -> Any:
    """Turn ChatGPT's always-SSE result into its completed Responses object."""
    if not hasattr(result, "__aiter__"):
        return result

    from litellm.responses.sse_output_recovery import (
        record_output_item_chunk,
        record_output_text_chunk,
    )
    from litellm.types.llms.openai import ResponsesAPIResponse

    completed: Any = None
    streamed_output_items: dict[int, dict[str, Any]] = {}
    text_only_output_items: dict[int, dict[str, Any]] = {}
    async for event in result:
        event_type = getattr(event, "type", None)
        if hasattr(event, "model_dump"):
            event_data = event.model_dump(exclude_none=True)
        elif isinstance(event, dict):
            event_data = event
        else:
            event_data = {}

        if event_type == "response.output_item.done":
            record_output_item_chunk(event_data, streamed_output_items)
        elif event_type == "response.output_text.done":
            record_output_text_chunk(
                event_data,
                streamed_output_items,
                text_only_output_items,
            )
        if event_type in {"response.completed", "response.incomplete"}:
            candidate = getattr(event, "response", None)
            if candidate is not None:
                completed = candidate

    if completed is None:
        final_event = getattr(result, "completed_response", None)
        completed = getattr(final_event, "response", None)
    if completed is None:
        raise RuntimeError("ChatGPT Responses stream ended without a completed response")

    # ChatGPT's final response event can omit output because the complete items
    # were delivered in preceding SSE events. Rehydrate a typed response using
    # the same recovery strategy as LiteLLM's own synchronous ChatGPT parser.
    merged_items = {**text_only_output_items, **streamed_output_items}
    if isinstance(completed, ResponsesAPIResponse) and not completed.output and merged_items:
        payload = completed.model_dump()
        payload["output"] = [item for _, item in sorted(merged_items.items())]
        completed = ResponsesAPIResponse(**payload)
    return completed


def _wrap_aresponses(
    original: Callable[..., Awaitable[Any]],
) -> Callable[..., Awaitable[Any]]:
    """Make an unexpected SSE result honor the caller's non-stream contract."""
    if getattr(original, _WRAPPER_MARKER, False):
        return original

    @wraps(original)
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        result = await original(*args, **kwargs)
        # The Anthropic adapter does not forward the resolved provider in every
        # LiteLLM call path. Key off the protocol mismatch itself: a requested
        # non-stream call that unexpectedly returns an async iterator.
        if not kwargs.get("stream", False):
            return await _completed_chatgpt_response(result)
        return result

    setattr(wrapped, _WRAPPER_MARKER, True)
    return wrapped


def _wrap_reasoning_effort_normalizer(
    original: Callable[[str, str, str | None], str],
) -> Callable[[str, str, str | None], str]:
    """Preserve GPT-5.6 effort values for private ChatGPT model slugs."""
    if getattr(original, _EFFORT_WRAPPER_MARKER, False):
        return original

    @wraps(original)
    def wrapped(effort: str, model: str, custom_llm_provider: str | None = None) -> str:
        if (
            custom_llm_provider == "chatgpt"
            and _is_gpt56_slug(model)
            and effort in _GPT56_REASONING_EFFORTS
        ):
            return effort
        return original(effort, model, custom_llm_provider)

    setattr(wrapped, _EFFORT_WRAPPER_MARKER, True)
    return wrapped


def _wrap_responses_tool_choice_translator(
    original: Callable[[Any], Any],
) -> Callable[[Any], Any]:
    """Emit Responses API scalar choices instead of invalid typed objects."""
    if getattr(original, _TOOL_CHOICE_WRAPPER_MARKER, False):
        return original

    @wraps(original)
    def wrapped(tool_choice: Any) -> Any:
        translated = original(tool_choice)
        if translated == {"type": "auto"}:
            return "auto"
        if translated == {"type": "required"}:
            return "required"
        return translated

    setattr(wrapped, _TOOL_CHOICE_WRAPPER_MARKER, True)
    return wrapped


def enable_chatgpt_responses_bridge() -> None:
    """Enable Anthropic→Responses, effort, and non-stream bridges for ChatGPT."""
    import litellm
    from litellm.llms.anthropic.experimental_pass_through import utils
    from litellm.llms.anthropic.experimental_pass_through.messages import handler
    from litellm.llms.anthropic.experimental_pass_through.responses_adapters.transformation import (
        LiteLLMAnthropicToResponsesAPIAdapter,
    )

    providers = handler._RESPONSES_API_PROVIDERS
    if not isinstance(providers, frozenset) or "openai" not in providers:
        raise RuntimeError(
            "the pinned LiteLLM Anthropic→Responses dispatch surface changed; "
            "review the compatibility shim before upgrading"
        )
    handler._RESPONSES_API_PROVIDERS = providers | {"chatgpt"}
    utils_module: Any = utils
    utils_module.normalize_reasoning_effort_value = _wrap_reasoning_effort_normalizer(
        utils.normalize_reasoning_effort_value
    )
    adapter_class: Any = LiteLLMAnthropicToResponsesAPIAdapter
    adapter_class.translate_tool_choice_to_responses_api = staticmethod(
        _wrap_responses_tool_choice_translator(
            LiteLLMAnthropicToResponsesAPIAdapter.translate_tool_choice_to_responses_api
        )
    )
    litellm.aresponses = _wrap_aresponses(litellm.aresponses)


def main() -> int:
    """Apply the pinned compatibility shim, then run LiteLLM's normal CLI."""
    enable_chatgpt_responses_bridge()

    from litellm import run_server  # type: ignore[attr-defined]

    result = _run_server(run_server)
    return result if isinstance(result, int) else 0


def _run_server(run_server: Callable[[], Any]) -> Any:
    """Small seam that keeps the CLI delegation independently testable."""
    return run_server()


if __name__ == "__main__":
    raise SystemExit(main())
