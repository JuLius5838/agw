"""Process identity and port helpers.

A bare PID is unsafe to signal: the OS reuses PIDs, so a recorded PID may later
belong to an unrelated process. We pair every PID with its ``create_time`` (from
psutil) to form a :class:`ProcessIdentity`; a process is "the same one" only when
both match. Every stop/terminate path checks identity before signalling.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass

import psutil

# create_time can differ by a hair between reads on some platforms.
_CREATE_TIME_EPSILON = 1.0


@dataclass(frozen=True)
class ProcessIdentity:
    """A PID paired with its creation time, robust against PID reuse."""

    pid: int
    create_time: float

    def to_dict(self) -> dict[str, object]:
        return {"pid": self.pid, "create_time": self.create_time}

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ProcessIdentity:
        return cls(pid=int(str(data["pid"])), create_time=float(str(data["create_time"])))


def identity_of(pid: int) -> ProcessIdentity | None:
    """Return the identity of a live PID, or ``None`` if it does not exist."""
    try:
        proc = psutil.Process(pid)
        return ProcessIdentity(pid=pid, create_time=proc.create_time())
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None


def current_identity() -> ProcessIdentity:
    """The identity of the current process."""
    import os

    identity = identity_of(os.getpid())
    if identity is None:  # pragma: no cover - the current process always exists
        raise RuntimeError("could not determine the current process identity")
    return identity


def is_alive(identity: ProcessIdentity) -> bool:
    """True iff a live process matches ``identity`` (PID *and* create_time)."""
    current = identity_of(identity.pid)
    if current is None:
        return False
    return abs(current.create_time - identity.create_time) <= _CREATE_TIME_EPSILON


def terminate(identity: ProcessIdentity, *, timeout: float = 5.0) -> bool:
    """Terminate the process only if it still matches ``identity``.

    Sends SIGTERM, waits, then SIGKILL if needed. Returns ``True`` once the
    identified process is gone. Never signals a PID whose create_time diverges
    (that would be a reused PID belonging to someone else).
    """
    if not is_alive(identity):
        return True
    try:
        proc = psutil.Process(identity.pid)
        if abs(proc.create_time() - identity.create_time) > _CREATE_TIME_EPSILON:
            return True  # PID reused by another process; leave it alone
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except psutil.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=timeout)
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        pass
    return not is_alive(identity)


def is_port_open(host: str, port: int, *, timeout: float = 0.5) -> bool:
    """True if something is accepting TCP connections on ``host:port``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def find_free_port(host: str = "127.0.0.1") -> int:
    """Bind an ephemeral port and return it (for tests / dynamic allocation)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])
