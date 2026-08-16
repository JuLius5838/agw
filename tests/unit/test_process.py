"""Unit tests for process identity and port helpers."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

import pytest

from agent_gateway.process import (
    ProcessIdentity,
    current_identity,
    find_free_port,
    identity_of,
    is_alive,
    is_port_open,
    terminate,
    terminate_process_group,
)

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")


def _spawn_sleeper(seconds: float = 60) -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


def _spawn_group_leader(seconds: float = 60) -> subprocess.Popen[bytes]:
    """A sleeper that leads its own session, like the probe's Claude process."""
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        start_new_session=True,
    )


def test_identity_roundtrips_through_dict():
    identity = current_identity()
    assert ProcessIdentity.from_dict(identity.to_dict()) == identity


def test_identity_of_implausible_pid_is_none():
    assert identity_of(2**31 - 1) is None


def test_live_process_is_alive_then_not():
    proc = _spawn_sleeper()
    identity = identity_of(proc.pid)
    assert identity is not None
    assert is_alive(identity)
    proc.terminate()
    proc.wait()
    assert is_alive(identity) is False


def test_stale_create_time_is_not_alive():
    identity = current_identity()
    stale = ProcessIdentity(pid=identity.pid, create_time=identity.create_time - 100_000)
    assert is_alive(stale) is False


def test_terminate_kills_matching_process():
    proc = _spawn_sleeper()
    identity = identity_of(proc.pid)
    assert identity is not None
    assert terminate(identity, timeout=5) is True
    assert is_alive(identity) is False
    proc.wait()


def test_terminate_stale_identity_does_not_kill_reused_pid():
    # An identity with the right PID but wrong create_time must be treated as
    # "already gone" and must never signal the live process holding that PID.
    proc = _spawn_sleeper(seconds=3)
    try:
        stale = ProcessIdentity(pid=proc.pid, create_time=1.0)
        assert terminate(stale) is True
        assert proc.poll() is None  # real process untouched
    finally:
        proc.terminate()
        proc.wait()


@posix_only
def test_process_group_termination_kills_the_whole_group():
    proc = _spawn_group_leader()
    identity = identity_of(proc.pid)
    assert identity is not None
    assert os.getpgid(proc.pid) == proc.pid  # it really leads its own group

    assert terminate_process_group(identity) is True
    proc.wait()  # reap, so the exited process is not merely a zombie
    assert is_alive(identity) is False


@posix_only
def test_process_group_termination_kills_a_child_in_the_group():
    # The real risk: Claude spawns hook processes, and a bare terminate() would
    # leave them orphaned. Signalling the group reaches them too.
    leader = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, sys, time; "
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            "print(child.pid, flush=True); time.sleep(60)",
        ],
        start_new_session=True,
        stdout=subprocess.PIPE,
    )
    try:
        assert leader.stdout is not None
        child_pid = int(leader.stdout.readline().decode().strip())
        identity = identity_of(leader.pid)
        assert identity is not None

        assert terminate_process_group(identity) is True

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and identity_of(child_pid) is not None:
            time.sleep(0.05)
        assert identity_of(child_pid) is None
    finally:
        if leader.poll() is None:  # pragma: no cover - cleanup safety net
            leader.kill()
        leader.wait()


@posix_only
def test_process_group_termination_never_signals_a_reused_pid():
    proc = _spawn_group_leader(seconds=3)
    try:
        stale = ProcessIdentity(pid=proc.pid, create_time=1.0)
        assert terminate_process_group(stale) is True
        assert proc.poll() is None  # the real process is untouched
    finally:
        proc.terminate()
        proc.wait()


@posix_only
def test_process_group_termination_of_a_dead_process_succeeds():
    proc = _spawn_group_leader(seconds=0.1)
    identity = identity_of(proc.pid)
    assert identity is not None
    proc.wait()
    time.sleep(0.1)
    assert terminate_process_group(identity) is True


def test_port_open_true_then_false():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]
    assert is_port_open("127.0.0.1", port) is True
    server.close()
    time.sleep(0.05)
    assert is_port_open("127.0.0.1", port) is False


def test_find_free_port_returns_int():
    port = find_free_port()
    assert isinstance(port, int)
    assert 1 <= port <= 65535
