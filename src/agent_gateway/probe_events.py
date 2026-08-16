"""Secure append-only NDJSON evidence for a native-subagent compatibility probe.

Two unrelated processes write here: a short-lived hook process spawned by Claude
Code, and the long-lived router. Together their records decide whether AGW will
advertise an external model, so the file is a trust boundary rather than a log.

Three properties carry that weight:

* **Validate before every write.** The file is re-checked for containment,
  symlinks, ownership, mode, type, and device/inode on each call, then re-checked
  through ``fstat`` on the descriptor actually opened. A file swapped between the
  check and the write is rejected rather than appended to.
* **One record, one syscall.** Each event is a single ``os.write`` of a complete
  line kept below ``PIPE_BUF``, which POSIX guarantees is atomic for an
  ``O_APPEND`` descriptor, so concurrent writers cannot interleave a line.
* **Say nothing on failure.** Every rejection raises one fixed message. The
  event body can hold repository detail and the path can hold local layout, so
  neither ever reaches a diagnostic.

This module imports no router, Claude, provider, or CLI code so both writers can
depend on it without a cycle.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from agent_gateway.errors import ProxyError

PROBE_EVENT_SCHEMA: Final[int] = 1

_NONCE_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}")
_DIR_MODE: Final[int] = 0o700
_FILE_MODE: Final[int] = 0o600
_MAX_READ_BYTES: Final[int] = 64 * 1024
_DEFAULT_PIPE_BUF: Final[int] = 512

_SAFETY_ERROR: Final[str] = "probe event file failed safety validation."
_CONTENT_ERROR: Final[str] = "probe event file is invalid."


@dataclass(frozen=True)
class ProbeFileIdentity:
    """The exact file a probe is allowed to touch.

    Recording device and inode (not just the path) is what makes a swapped or
    recreated file detectable: the path can be reused by anyone who can write the
    directory, but the identity cannot.
    """

    path: str
    device: int
    inode: int
    uid: int
    mode: int


def _unsafe() -> ProxyError:
    return ProxyError(
        _SAFETY_ERROR,
        hint="Remove stale probe state and re-run verification.",
    )


def _validate_directory(probe_dir: Path) -> Path:
    """Require the dedicated directory itself to be a private real directory."""
    try:
        info = probe_dir.lstat()
    except OSError as exc:
        raise _unsafe() from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise _unsafe()
    if info.st_uid != os.getuid():
        raise _unsafe()
    if os.name == "posix" and stat.S_IMODE(info.st_mode) != _DIR_MODE:
        raise _unsafe()
    return probe_dir


def validate_probe_file(probe_dir: Path, identity: ProbeFileIdentity) -> None:
    """Raise unless ``identity`` still names the exact expected private file.

    Returns nothing and opens nothing: callers open their own descriptor and
    re-validate it, so there is no descriptor here for a caller to leak.
    """
    directory = _validate_directory(probe_dir)
    path = Path(identity.path)
    if path.parent != directory:
        raise _unsafe()
    try:
        info = path.lstat()
    except OSError as exc:
        raise _unsafe() from exc
    _require_matching(info, identity)


def _require_matching(info: os.stat_result, identity: ProbeFileIdentity) -> None:
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise _unsafe()
    if info.st_uid != os.getuid() or info.st_uid != identity.uid:
        raise _unsafe()
    if os.name == "posix" and stat.S_IMODE(info.st_mode) != _FILE_MODE:
        raise _unsafe()
    if stat.S_IMODE(identity.mode) != _FILE_MODE:
        raise _unsafe()
    if info.st_dev != identity.device or info.st_ino != identity.inode:
        raise _unsafe()


def _open_validated(probe_dir: Path, identity: ProbeFileIdentity, flags: int) -> int:
    """Open the validated path and confirm the descriptor is that same file."""
    validate_probe_file(probe_dir, identity)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    try:
        handle = os.open(identity.path, flags | no_follow)
    except OSError as exc:
        raise _unsafe() from exc
    try:
        # Close the check-to-use window: the descriptor, not the path, is what
        # the write lands on, so it must match the identity too.
        _require_matching(os.fstat(handle), identity)
        _require_matching(Path(identity.path).lstat(), identity)
    except BaseException:
        os.close(handle)
        raise
    return handle


def _pipe_buf(handle: int) -> int:
    try:
        return int(os.fpathconf(handle, "PC_PIPE_BUF"))
    except (OSError, ValueError, AttributeError):  # pragma: no cover - platform fallback
        return _DEFAULT_PIPE_BUF


def create_probe_file(probe_dir: Path, nonce: str) -> ProbeFileIdentity:
    """Create one private, empty event file and capture its identity."""
    if not _NONCE_PATTERN.fullmatch(nonce):
        raise _unsafe()
    directory = _validate_directory(probe_dir)
    path = directory / f"{nonce}.ndjson"
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    try:
        # O_EXCL refuses an existing path outright: reusing one would let an
        # attacker pre-create the file, and truncating one would erase evidence.
        handle = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND | no_follow,
            _FILE_MODE,
        )
    except OSError as exc:
        raise _unsafe() from exc
    try:
        info = os.fstat(handle)
    finally:
        os.close(handle)
    return ProbeFileIdentity(
        path=str(path),
        device=info.st_dev,
        inode=info.st_ino,
        uid=info.st_uid,
        mode=stat.S_IMODE(info.st_mode),
    )


def append_probe_event(
    probe_dir: Path,
    identity: ProbeFileIdentity,
    event: Mapping[str, object],
) -> None:
    """Append one canonical NDJSON record with a single atomic write."""
    if not isinstance(event, Mapping):
        raise _unsafe()
    try:
        record = json.dumps(
            dict(event),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
    except (TypeError, ValueError) as exc:
        raise _unsafe() from exc
    payload = f"{record}\n".encode()

    handle = _open_validated(probe_dir, identity, os.O_WRONLY | os.O_APPEND)
    try:
        if len(payload) >= _pipe_buf(handle):
            # Beyond PIPE_BUF the kernel may split the write, letting a
            # concurrent writer interleave and corrupt both records.
            raise _unsafe()
        if os.write(handle, payload) != len(payload):  # pragma: no cover - short write
            raise _unsafe()
    finally:
        os.close(handle)


def read_probe_events(
    probe_dir: Path,
    identity: ProbeFileIdentity,
) -> tuple[dict[str, object], ...]:
    """Read every event, refusing anything malformed rather than repairing it."""
    handle = _open_validated(probe_dir, identity, os.O_RDONLY)
    try:
        if os.fstat(handle).st_size > _MAX_READ_BYTES:
            raise ProxyError(_CONTENT_ERROR)
        raw = os.read(handle, _MAX_READ_BYTES)
    finally:
        os.close(handle)

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProxyError(_CONTENT_ERROR) from exc

    if not text:
        return ()
    if not text.endswith("\n"):
        # A complete record always ends in a newline, so a dangling tail means
        # the file was truncated mid-write. Never guess at the missing bytes.
        raise ProxyError(_CONTENT_ERROR)

    events: list[dict[str, object]] = []
    for line in text.split("\n")[:-1]:
        if not line:
            raise ProxyError(_CONTENT_ERROR)
        try:
            parsed: Any = json.loads(line)
        except ValueError as exc:
            raise ProxyError(_CONTENT_ERROR) from exc
        if not isinstance(parsed, dict):
            raise ProxyError(_CONTENT_ERROR)
        events.append({str(key): value for key, value in parsed.items()})
    return tuple(events)


def remove_probe_file(probe_dir: Path, identity: ProbeFileIdentity) -> None:
    """Delete the event file, but only while it is still the same file.

    Idempotent when already gone. A path now holding a *different* file is left
    alone and reported, because deleting it would destroy someone else's data.
    """
    try:
        _validate_directory(probe_dir)
    except ProxyError:
        raise
    path = Path(identity.path)
    if path.parent != probe_dir:
        raise _unsafe()
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise _unsafe() from exc
    if info.st_dev != identity.device or info.st_ino != identity.inode:
        raise _unsafe()
    try:
        path.unlink()
    except FileNotFoundError:  # pragma: no cover - lost a benign race
        return
    except OSError as exc:
        raise _unsafe() from exc


__all__ = [
    "PROBE_EVENT_SCHEMA",
    "ProbeFileIdentity",
    "append_probe_event",
    "create_probe_file",
    "read_probe_events",
    "remove_probe_file",
    "validate_probe_file",
]
