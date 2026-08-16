"""Live smoke tests — require REAL provider OAuth. Skipped unless run with `-m live`.

    uv run agw setup
    uv run agw auth <provider>  # chatgpt or copilot for every enabled model
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


# --------------------------------------------------------------------------- #
# External subagents (Gate C) — live only, never part of the default suite
# --------------------------------------------------------------------------- #
def _external_models(paths: Paths, registry: ModelRegistry) -> list[str]:
    authed = _authed_providers(paths, registry)
    return [entry.name for entry in registry.active_models() if entry.provider in authed]


def test_native_subagent_verification_records_current_evidence(running_proxy) -> None:
    """Two models must pass the real probe and store evidence bound to this build."""
    from agent_gateway.claude_compat import (
        CompatibilityIdentity,
        CompatibilityState,
        compatibility_status,
        resolve_claude_identity,
    )
    from agent_gateway.config import read_claude_settings
    from agent_gateway.external_subagents import DEFINITION_CONTRACT_VERSION
    from agent_gateway.secret_store import ensure_proxy_key
    from agent_gateway.subagent_probe import verify_native_subagent

    paths, config, registry, state = running_proxy
    models = _external_models(paths, registry)
    if len(models) < 2:
        pytest.skip("enable and authenticate at least two external models first")

    key = ensure_proxy_key(paths)
    claude = resolve_claude_identity(config)
    for name in models[:2]:
        entry = registry.get_active(name)
        report = verify_native_subagent(paths, config, registry, state, key, entry)
        assert report.ok, f"{name}: {report.detail}"

        identity = CompatibilityIdentity(
            model=entry.name,
            provider=entry.provider,
            claude=claude,
            runtime_fingerprint=state.runtime_fingerprint,
            definition_contract=DEFINITION_CONTRACT_VERSION,
        )
        status = compatibility_status(paths, identity, settings=read_claude_settings())
        assert status.state is CompatibilityState.current
        record = status.record
        assert record is not None
        assert record.identity.claude.version == claude.version
        assert record.identity.claude.sha256 == claude.sha256


def test_verified_models_are_advertised_as_generated_types(running_proxy) -> None:
    """A managed launch must inject exactly the current models, and nothing else."""
    from agent_gateway.claude_compat import resolve_claude_identity
    from agent_gateway.config import read_claude_settings
    from agent_gateway.external_subagents import current_generated_agents

    paths, config, registry, state = running_proxy
    agents = current_generated_agents(
        paths,
        registry,
        resolve_claude_identity(config),
        state.runtime_fingerprint,
        read_claude_settings(),
    )
    if len(agents) < 2:
        pytest.skip("run the verification test first")

    for agent in agents:
        assert agent.agent_type.startswith("agw-")
        assert agent.definition["model"] == agent.model
        assert agent.definition["tools"] == ["Read", "Grep", "Glob"]


def test_probe_state_is_removed_and_evidence_holds_no_content(running_proxy) -> None:
    """After verification, no probe artifact remains and no canary was persisted."""
    paths, _config, _registry, _state = running_proxy
    if paths.probe_dir.exists():
        assert list(paths.probe_dir.iterdir()) == []

    for record in paths.compatibility_dir.glob("*/*.json"):
        text = record.read_text(encoding="utf-8").lower()
        for forbidden in ("prompt", "authorization", "bearer", "sk-", "token", "fixture"):
            assert forbidden not in text
    if paths.proxy_log.exists():
        log = paths.proxy_log.read_text(encoding="utf-8").lower()
        assert "agw-probe-" not in log  # the fixture canary never reaches the log


@pytest.mark.skip(
    reason=(
        "Operator-driven: run a managed `claude` session, ask for a multi-model review, "
        "and record the checklist in tests/manual/README.md."
    )
)
def test_parent_runs_two_external_types_concurrently_and_synthesizes() -> None:
    """Concurrency, synthesis, cancellation, and provider failure are observed by hand.

    These need a real interactive parent session, so they are a documented operator
    checklist rather than an automated assertion. See the Gate C list in
    tests/manual/README.md.
    """
