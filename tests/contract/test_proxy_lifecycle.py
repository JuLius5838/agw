"""Native-only lifecycle against a real detached supervisor and front router.

Exercises start/reuse/stop, stale-state recovery, port conflict, fingerprint
mismatch, and 10-way concurrency as separate processes (the real launcher
scenario), without provider credentials or network access.
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import psutil
import pytest

from agent_gateway import proxy
from agent_gateway.config import GatewayConfig, save_config
from agent_gateway.errors import PortConflictError, ProxyError
from agent_gateway.model_registry import ModelRegistry, load_registry_text
from agent_gateway.paths import Paths, get_paths
from agent_gateway.process import find_free_port, is_alive
from agent_gateway.proxy import ensure_running, read_state, write_state

pytestmark = pytest.mark.contract

REGISTRY_TEXT = """
default_model: null
models: []
"""


def _seed(home: Path) -> tuple[Paths, GatewayConfig, ModelRegistry]:
    paths = get_paths({"HOME": str(home)})
    config = GatewayConfig(port=find_free_port())
    save_config(paths, config)
    paths.models_file.write_text(REGISTRY_TEXT)
    return paths, config, load_registry_text(REGISTRY_TEXT)


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[tuple[Paths, GatewayConfig, ModelRegistry]]:
    paths, config, registry = _seed(tmp_path)
    try:
        yield paths, config, registry
    finally:
        with contextlib.suppress(Exception):
            proxy.stop(paths)


def test_ensure_running_starts_then_reuses(runtime) -> None:
    paths, config, registry = runtime
    first = ensure_running(paths, config, registry)
    assert proxy.status(paths).healthy is True

    second = ensure_running(paths, config, registry)
    assert second.supervisor == first.supervisor  # same daemon reused
    assert second.router == first.router
    assert second.litellm == first.litellm


def test_stop_is_idempotent(runtime) -> None:
    paths, config, registry = runtime
    ensure_running(paths, config, registry)
    assert proxy.stop(paths) is True
    assert proxy.status(paths).running is False
    assert proxy.stop(paths) is False


def test_stale_state_is_recovered(runtime) -> None:
    paths, config, registry = runtime
    first = ensure_running(paths, config, registry)

    # Simulate a crash: SIGKILL the managed processes, leaving proxy.json on disk.
    assert first.litellm is None
    os.kill(first.router.pid, signal.SIGKILL)
    os.kill(first.supervisor.pid, signal.SIGKILL)
    time.sleep(0.3)
    assert read_state(paths) is not None  # stale file remains

    second = ensure_running(paths, config, registry)
    assert second.supervisor.pid != first.supervisor.pid
    assert proxy.status(paths).healthy is True


def test_port_conflict_is_reported(runtime) -> None:
    paths, _config, registry = runtime
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    conflicting_port = blocker.getsockname()[1]
    try:
        config = GatewayConfig(port=conflicting_port)
        with pytest.raises(PortConflictError):
            ensure_running(paths, config, registry)
    finally:
        blocker.close()


def test_fingerprint_mismatch_requires_explicit_restart(runtime) -> None:
    paths, config, registry = runtime
    state = ensure_running(paths, config, registry)

    # Simulate a runtime/config upgrade under an active proxy.
    mutated = proxy.ProxyState(
        supervisor=state.supervisor,
        router=state.router,
        litellm=state.litellm,
        host=state.host,
        port=state.port,
        litellm_host=state.litellm_host,
        litellm_port=state.litellm_port,
        litellm_version=state.litellm_version,
        config_fingerprint=state.config_fingerprint,
        runtime_fingerprint="different-fingerprint",
        log_path=state.log_path,
    )
    write_state(paths.proxy_state_file, mutated)

    with pytest.raises(ProxyError, match="different config/version") as excinfo:
        ensure_running(paths, config, registry)
    assert "restart" in (excinfo.value.hint or "")


def test_runtime_abi_change_invalidates_fingerprint(monkeypatch) -> None:
    rendered = "model_list: []\n"
    before = proxy.runtime_fingerprint(rendered)

    monkeypatch.setattr(proxy, "RUNTIME_ABI", "next-runtime-contract")

    assert proxy.runtime_fingerprint(rendered) != before


def _child_start(home_str: str) -> tuple[int, str]:
    """Run `agw proxy start` in a subprocess; return (exit code, stderr tail).

    stderr is returned so a concurrency failure reports *why* a child failed.
    """
    agw = Path(sys.executable).parent / "agw"
    env = {**os.environ, "HOME": home_str}
    env.pop("XDG_CONFIG_HOME", None)
    env.pop("XDG_STATE_HOME", None)
    completed = subprocess.run(
        [str(agw), "proxy", "start"],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    return completed.returncode, completed.stderr.strip()[-200:]


def _count_supervisors(state_file: Path) -> int:
    count = 0
    for process in psutil.process_iter(["cmdline"]):
        try:
            cmdline = " ".join(process.info["cmdline"] or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if "agent_gateway.supervisor" in cmdline and str(state_file) in cmdline:
            count += 1
    return count


def test_ten_concurrent_launches_yield_one_daemon(tmp_path: Path) -> None:
    paths, _config, _registry = _seed(tmp_path)
    try:
        with ProcessPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(_child_start, [str(tmp_path)] * 10))
        assert all(code == 0 for code, _ in results), results

        state = read_state(paths)
        assert state is not None
        assert is_alive(state.supervisor)
        # Exactly one supervisor process is bound to this test's state file.
        assert _count_supervisors(paths.proxy_state_file) == 1
    finally:
        with contextlib.suppress(Exception):
            proxy.stop(paths)
