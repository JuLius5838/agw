"""Unit tests for sanitized operational logging (allowlist + redaction)."""

from __future__ import annotations

from agent_gateway.observability import format_event, sanitize_litellm_line


def test_keeps_lifecycle_lines():
    assert sanitize_litellm_line("INFO:     Application startup complete.\n") is not None
    assert sanitize_litellm_line("INFO: Uvicorn running on http://127.0.0.1:4000\n") is not None
    assert sanitize_litellm_line("Started server process [123]\n") is not None


def test_keeps_error_lines():
    assert sanitize_litellm_line("LiteLLM Proxy:ERROR - something failed\n") is not None
    assert sanitize_litellm_line("ModuleNotFoundError: No module named 'prisma'\n") is not None


def test_drops_unknown_and_empty_lines():
    assert sanitize_litellm_line("GET /v1/messages body={'system': 'you are ...'}\n") is None
    assert sanitize_litellm_line("just some chatter\n") is None
    assert sanitize_litellm_line("\n") is None
    assert sanitize_litellm_line("   ") is None


def test_drops_device_code_and_signin_lines():
    assert sanitize_litellm_line("Sign in with ChatGPT using device code: WDJB-MJHT\n") is None
    assert sanitize_litellm_line("Enter code: ABCD-1234\n") is None
    assert sanitize_litellm_line('{"user_code": "ABCD"} error\n') is None  # deny beats allow


def test_redacts_secrets_in_kept_lines():
    kept = sanitize_litellm_line("ERROR: Authorization: Bearer sk-secret-abcdef123 rejected\n")
    assert kept is not None
    assert "sk-secret-abcdef123" not in kept


def test_canary_prompt_line_is_dropped():
    assert sanitize_litellm_line("CANARY-PROMPT-9f3a summarize the following secret memo\n") is None


def test_format_event_prefixes_and_redacts():
    out = format_event("leaked proxy key sk-agw-abcdef123456 oops")
    assert "agw-supervisor" in out
    assert "sk-agw-abcdef123456" not in out
