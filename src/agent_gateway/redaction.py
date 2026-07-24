"""Central redaction of secrets before anything is logged, printed, or persisted.

Two complementary strategies:

1. **Structural patterns** — regexes for well-known secret shapes (bearer tokens,
   ``sk-`` keys, GitHub tokens, JWTs) and for sensitive ``key: value`` /
   ``"key": "value"`` fields in log/JSON/YAML text.
2. **Exact known secrets** — a :class:`Redactor` can be seeded with the literal
   secret values the process is holding (the proxy key, a device code, an OAuth
   token) so they are masked wherever they appear, even in an unexpected shape.

Redaction is deliberately conservative about what it masks so that legitimate
routing evidence — public model names like ``gpt-5.3-codex`` and provider names
like ``chatgpt`` — is preserved for operational logs.
"""

from __future__ import annotations

import re

REDACTED = "«redacted»"

# Sensitive field names (used for key: value and "key": "value" redaction).
_SENSITIVE_KEYS = (
    "authorization",
    "api[_-]?key",
    "access[_-]?token",
    "refresh[_-]?token",
    "id[_-]?token",
    "client[_-]?secret",
    "proxy[_-]?key",
    "master[_-]?key",
    "password",
    "passwd",
    "secret",
    "bearer",
    "device[_-]?code",
    "user[_-]?code",
    "token",
)
_SENSITIVE_ALT = "|".join(_SENSITIVE_KEYS)

# Structural secret-shape patterns.
_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Authorization: Bearer <token>  /  "Bearer <token>"
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    # OpenAI / LiteLLM style keys: sk-...  (our proxy key is sk-agw-...)
    re.compile(r"\bsk-[A-Za-z0-9._-]{6,}"),
    # GitHub tokens: gho_, ghp_, ghu_, ghs_, ghr_ + 20+ chars
    re.compile(r"\bgh[oupsr]_[A-Za-z0-9]{20,}"),
    # JSON Web Tokens (three base64url segments).
    re.compile(r"\beyJ[A-Za-z0-9._-]{10,}"),
)

# "key": "value"   (JSON-ish)
_JSON_KV = re.compile(rf'(?i)("(?:{_SENSITIVE_ALT})"\s*:\s*)"[^"]*"')
# key: value  or  key = value   (YAML / env / header-ish), value to end of line
_LINE_KV = re.compile(rf"(?im)^(\s*(?:{_SENSITIVE_ALT})\s*[:=]\s*).+$")
# Authorization header inline form:  Authorization: <anything>
_HEADER_KV = re.compile(r"(?i)(authorization\s*:\s*).+")


def _apply_patterns(text: str) -> str:
    text = _JSON_KV.sub(rf'\1"{REDACTED}"', text)
    text = _LINE_KV.sub(rf"\1{REDACTED}", text)
    text = _HEADER_KV.sub(rf"\1{REDACTED}", text)
    for pattern in _VALUE_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def redact(text: str) -> str:
    """Redact well-known secret shapes and sensitive fields from ``text``."""
    return _apply_patterns(text)


class Redactor:
    """Redacts structural secret shapes *and* a set of exact known secret values."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()

    def add_secret(self, value: str | None, *, min_length: int = 4) -> None:
        """Register a literal secret to mask everywhere. Short/empty values ignored."""
        if value and len(value) >= min_length:
            self._secrets.add(value)

    def redact(self, text: str) -> str:
        # Mask exact secrets first (longest first, so a token isn't partially masked).
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, REDACTED)
        return _apply_patterns(text)

    def redact_mapping(self, mapping: dict[str, object]) -> dict[str, object]:
        """Return a shallow copy with sensitive keys and secret values redacted."""
        sensitive = re.compile(rf"(?i)^(?:{_SENSITIVE_ALT})$")
        result: dict[str, object] = {}
        for key, value in mapping.items():
            if sensitive.match(key):
                result[key] = REDACTED
            elif isinstance(value, str):
                result[key] = self.redact(value)
            else:
                result[key] = value
        return result
