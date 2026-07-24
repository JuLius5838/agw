"""Sanitized operational logging for the managed proxy.

The supervisor drains LiteLLM's stdout/stderr continuously but must persist *only*
allowlisted lifecycle/error metadata, and only after redaction. Anything that does
not match the allowlist is dropped rather than risking prompt/secret persistence.
The supervisor's own structured events are always safe to persist.
"""

from __future__ import annotations

import re
import time

from agent_gateway.redaction import redact

# Lines from LiteLLM we are willing to persist (lifecycle + error category only).
# Prompt/response bodies are already disabled in the generated config; this is a
# second, allowlist-based line of defense.
_ALLOWED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"application startup complete", re.IGNORECASE),
    re.compile(r"waiting for application startup", re.IGNORECASE),
    re.compile(r"uvicorn running", re.IGNORECASE),
    re.compile(r"shutting down", re.IGNORECASE),
    re.compile(r"started server process", re.IGNORECASE),
    re.compile(r"traceback \(most recent call last\)", re.IGNORECASE),
    # Substring (not word-bounded) so compound names like "ModuleNotFoundError"
    # are captured. Prompt bodies are never in the log stream to begin with.
    re.compile(r"error", re.IGNORECASE),
    re.compile(r"exception", re.IGNORECASE),
    re.compile(r"critical", re.IGNORECASE),
)

# Never persist these even if they otherwise match (device codes, sign-in prompts).
_DENY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sign in", re.IGNORECASE),
    re.compile(r"device code", re.IGNORECASE),
    re.compile(r"enter code", re.IGNORECASE),
    re.compile(r"user_code", re.IGNORECASE),
)


def sanitize_litellm_line(line: str) -> str | None:
    """Return a redacted line safe to persist, or ``None`` to drop it."""
    stripped = line.rstrip("\n")
    if not stripped.strip():
        return None
    if any(pattern.search(stripped) for pattern in _DENY_PATTERNS):
        return None
    if not any(pattern.search(stripped) for pattern in _ALLOWED_PATTERNS):
        return None
    return redact(stripped)


def format_event(message: str) -> str:
    """Format a supervisor-generated event line (timestamped, redacted)."""
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
    return f"{timestamp} agw-supervisor: {redact(message)}"
