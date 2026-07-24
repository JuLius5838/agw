"""Storage for the local loopback proxy key.

The proxy key is the *only* secret the gateway itself generates. It is a local
loopback credential (not an upstream billing credential): it authenticates Claude
Code to the local LiteLLM proxy and is passed to LiteLLM via its process
environment. It is generated once, reused on subsequent setup runs, and stored
``0600`` under the ``0700`` credentials directory.
"""

from __future__ import annotations

import secrets

from agent_gateway.paths import (
    SECRET_FILE_MODE,
    Paths,
    atomic_write_text,
    ensure_dir,
    read_text,
)

_PROXY_KEY_PREFIX = "sk-agw-"


def generate_proxy_key() -> str:
    """Return a fresh, cryptographically random loopback proxy key."""
    return _PROXY_KEY_PREFIX + secrets.token_urlsafe(32)


def read_proxy_key(paths: Paths) -> str | None:
    """Return the stored proxy key, or ``None`` if it has not been generated yet."""
    path = paths.proxy_key_file
    if not path.exists():
        return None
    return read_text(path).strip()


def ensure_proxy_key(paths: Paths) -> str:
    """Return the stored proxy key, generating and persisting one on first use.

    Idempotent: repeated calls return the same key so re-running setup never
    invalidates a live proxy or already-launched Claude session.
    """
    existing = read_proxy_key(paths)
    if existing:
        return existing
    ensure_dir(paths.credentials_dir)
    key = generate_proxy_key()
    atomic_write_text(paths.proxy_key_file, key + "\n", mode=SECRET_FILE_MODE)
    return key
