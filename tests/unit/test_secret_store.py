"""Unit tests for the local loopback proxy-key store."""

from __future__ import annotations

import os
import stat

import pytest

from agent_gateway.paths import get_paths
from agent_gateway.secret_store import ensure_proxy_key, generate_proxy_key, read_proxy_key

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX permission semantics")


def test_generated_key_shape():
    key = generate_proxy_key()
    assert key.startswith("sk-agw-")
    assert len(key) > 24


def test_ensure_generates_then_is_idempotent(tmp_path):
    p = get_paths({"HOME": str(tmp_path)})
    assert read_proxy_key(p) is None
    first = ensure_proxy_key(p)
    assert first.startswith("sk-agw-")
    # Re-running setup must reuse the same key, never rotate it out from under a
    # running proxy or Claude session.
    second = ensure_proxy_key(p)
    assert first == second
    assert read_proxy_key(p) == first


@posix_only
def test_proxy_key_and_dir_permissions(tmp_path):
    p = get_paths({"HOME": str(tmp_path)})
    ensure_proxy_key(p)
    assert stat.S_IMODE(p.proxy_key_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(p.credentials_dir.stat().st_mode) == 0o700
