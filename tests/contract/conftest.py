"""Shared fixtures for contract tests that spawn real proxy processes.

Contract tests boot real ``litellm``/supervisor processes. A test that crashes or
SIGKILLs a supervisor can orphan its ``litellm`` child; left running, that orphan
holds a port and can perturb a later test. The autouse reaper below records the
relevant processes before each test and terminates any that this test newly
created but did not clean up — keeping the suite hermetic locally and in CI.
"""

from __future__ import annotations

from collections.abc import Iterator

import psutil
import pytest


def _gateway_pids() -> set[int]:
    pids: set[int] = set()
    for process in psutil.process_iter(["cmdline"]):
        try:
            cmdline = " ".join(process.info["cmdline"] or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        if (
            "agent_gateway.supervisor" in cmdline
            or "agent_gateway.router" in cmdline
            or ("litellm" in cmdline and "--config" in cmdline)
        ):
            pids.add(process.pid)
    return pids


@pytest.fixture(autouse=True)
def reap_orphan_daemons() -> Iterator[None]:
    before = _gateway_pids()
    try:
        yield
    finally:
        leaked = _gateway_pids() - before
        for pid in leaked:
            try:
                proc = psutil.Process(pid)
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except psutil.TimeoutExpired:
                    proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
