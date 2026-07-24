"""Live smoke tests — require REAL provider OAuth. Skipped unless run with `-m live`.

    uv run agw setup && uv run agw auth chatgpt
    uv run pytest tests/manual -m live -v

These exercise the one thing the offline suite cannot: a real upstream response through
the managed proxy. They use throwaway prompts and assert only on sanitized, structural
outcomes — never printing prompt bodies, tokens, or device codes.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

from agent_gateway import proxy
from agent_gateway.config import GatewayConfig, load_config
from agent_gateway.model_registry import ModelRegistry, load_registry
from agent_gateway.models import verify_model
from agent_gateway.paths import Paths, get_paths
from agent_gateway.providers import Provider
from agent_gateway.providers.base import AuthStatus
from agent_gateway.proxy import ProxyState
from agent_gateway.secret_store import read_proxy_key

pytestmark = pytest.mark.live

Runtime = tuple[Paths, GatewayConfig, ModelRegistry, ProxyState, str]


@pytest.fixture(scope="module")
def running_proxy() -> Iterator[Runtime]:
    paths = get_paths()
    config = load_config(paths)
    registry = load_registry(paths)
    key = read_proxy_key(paths)
    if key is None:
        pytest.skip("run `agw setup` first")
    state = proxy.ensure_running(paths, config, registry)
    yield paths, config, registry, state, key


def _authed_providers(paths: Paths, registry: ModelRegistry) -> set[Provider]:
    from agent_gateway.auth import get_adapter

    ok: set[Provider] = set()
    for entry in registry.active_models():
        adapter = get_adapter(entry.provider)
        if adapter.auth_state(paths).status is AuthStatus.authenticated:
            ok.add(entry.provider)
    return ok


def test_default_model_streams_a_real_response(running_proxy) -> None:
    paths, config, registry, state, key = running_proxy
    if not _authed_providers(paths, registry):
        pytest.skip("no authenticated provider; run `agw auth <provider>`")

    default = registry.default_entry()
    if default is None:
        pytest.skip("no external default model configured")
    if default.provider not in _authed_providers(paths, registry):
        pytest.skip(f"default provider {default.provider.value} not authenticated")

    with httpx.stream(
        "POST",
        f"{state.url}/v1/messages",
        headers={
            "Authorization": f"Bearer {key}",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": default.name,
            "max_tokens": 16,
            "stream": True,
            "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        },
        timeout=60.0,
    ) as resp:
        assert resp.status_code == 200
        saw_start = saw_stop = False
        for line in resp.iter_lines():
            saw_start = saw_start or "message_start" in line
            saw_stop = saw_stop or "message_stop" in line
    assert saw_start and saw_stop  # clean SSE; content intentionally not inspected


def test_verify_default_model_full_contract(running_proxy) -> None:
    paths, config, registry, state, key = running_proxy
    default = registry.default_entry()
    if default is None:
        pytest.skip("no external default model configured")
    if default.provider not in _authed_providers(paths, registry):
        pytest.skip(f"default provider {default.provider.value} not authenticated")
    report = verify_model(state, key, default)
    failed = [c.name for c in report.checks if c.status.value == "failed"]
    assert report.ok, f"failed checks: {failed}"
