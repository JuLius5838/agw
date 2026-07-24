"""Secret-hygiene guardrails (AC-11).

Two things must hold:
  * no source/config file tracked in the repo contains a secret-shaped value, and
  * the supervisor's *persisted raw log file* never contains canary prompts or
    secrets — inspected on disk directly, not through the filtered `agw logs`.
"""

from __future__ import annotations

import re
from pathlib import Path

from agent_gateway.supervisor import _Log

REPO_ROOT = Path(__file__).resolve().parents[2]

# Shipped artifacts: the code, config, scripts, skills, and manifests that go out
# in a release. The `tests/` tree is intentionally excluded because it holds
# deliberately fake secret-shaped fixtures for the redaction tests; gitleaks (in
# CI) scans the whole tree with an allowlist for those fixtures.
_SHIPPED_ROOTS = ("src", "scripts", "skills", "config", ".claude-plugin")
_SHIPPED_ROOT_FILES = ("pyproject.toml", "README.md")

# Secret-shaped patterns that must never appear in shipped artifacts.
_SECRET_PATTERNS = (
    re.compile(r"\bgh[oupsr]_[A-Za-z0-9]{20,}"),  # GitHub tokens
    re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\."),  # JWT-ish
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),  # private keys
)

_TEXT_SUFFIXES = {".py", ".toml", ".yaml", ".yml", ".json", ".sh", ".md"}


def _shipped_text_files() -> list[Path]:
    files: list[Path] = []
    for root in _SHIPPED_ROOTS:
        for path in (REPO_ROOT / root).rglob("*"):
            if "__pycache__" in path.parts:
                continue
            if path.is_file() and path.suffix in _TEXT_SUFFIXES:
                files.append(path)
    for name in _SHIPPED_ROOT_FILES:
        candidate = REPO_ROOT / name
        if candidate.is_file():
            files.append(candidate)
    return files


def test_no_secret_shaped_values_in_tracked_files() -> None:
    offenders: list[str] = []
    for path in _shipped_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for pattern in _SECRET_PATTERNS:
            if pattern.search(text):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {pattern.pattern}")
    assert not offenders, f"secret-shaped values found: {offenders}"


def test_no_real_proxy_key_in_tracked_files() -> None:
    # An sk-agw-... value with real entropy must never be committed. (Short
    # placeholders like "sk-agw-key" in tests are fine.)
    real_key = re.compile(r"sk-agw-[A-Za-z0-9_-]{30,}")
    for path in _shipped_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        assert not real_key.search(text), f"possible real proxy key in {path}"


def test_raw_supervisor_log_drops_canary_prompt_and_secrets(tmp_path) -> None:
    log_file = tmp_path / "logs" / "proxy.log"
    log = _Log(log_file)
    # Simulate the kinds of lines LiteLLM might emit; only lifecycle/error survive.
    log.litellm("POST /v1/messages body={'system':'CANARY-PROMPT summarize secret memo'}\n")
    log.litellm("Authorization: Bearer sk-agw-REALKEY1234567890abcdefghij\n")
    log.litellm("Sign in with device code: WDJB-MJHT\n")
    log.litellm("INFO: Application startup complete.\n")  # this one is allowed
    log.event("proxy started")
    log.close()

    raw = log_file.read_text()  # inspect the persisted file directly
    assert "CANARY-PROMPT" not in raw
    assert "sk-agw-REALKEY1234567890abcdefghij" not in raw
    assert "WDJB-MJHT" not in raw
    assert "Application startup complete" in raw  # lifecycle preserved
