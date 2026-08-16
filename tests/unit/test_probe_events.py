"""Unit tests for the secure append-only probe event file.

The router process and a short-lived hook process both append to one file that
decides whether a model is advertised, so the file is a trust boundary. Every
append re-validates containment, ownership, mode, type, and device/inode before
writing, and each record is a single ``os.write`` below ``PIPE_BUF`` so two
writers can never interleave a line.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from agent_gateway.errors import ProxyError
from agent_gateway.paths import ensure_dir
from agent_gateway.probe_events import (
    PROBE_EVENT_SCHEMA,
    ProbeFileIdentity,
    append_probe_event,
    create_probe_file,
    read_probe_events,
    remove_probe_file,
    validate_probe_file,
)

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX permission semantics")

NONCE = "a" * 32
SAFETY_ERROR = "probe event file failed safety validation."


@pytest.fixture
def probe_dir(tmp_path) -> Path:
    return ensure_dir(tmp_path / "probes", 0o700)


@pytest.fixture
def identity(probe_dir) -> ProbeFileIdentity:
    return create_probe_file(probe_dir, NONCE)


# --------------------------------------------------------------------------- #
# Creation and the happy path
# --------------------------------------------------------------------------- #
def test_create_and_append_probe_event_is_restrictive(probe_dir):
    identity = create_probe_file(probe_dir, NONCE)
    assert stat.S_IMODE(probe_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(Path(identity.path).stat().st_mode) == 0o600
    append_probe_event(probe_dir, identity, {"schema": 1, "event": "pre_tool_use"})
    assert read_probe_events(probe_dir, identity) == ({"schema": 1, "event": "pre_tool_use"},)


def test_created_path_is_the_exact_nonce_file(probe_dir):
    identity = create_probe_file(probe_dir, NONCE)
    assert Path(identity.path) == probe_dir / f"{NONCE}.ndjson"
    assert identity.uid == os.getuid()
    assert stat.S_IMODE(identity.mode) == 0o600
    assert PROBE_EVENT_SCHEMA == 1


@pytest.mark.parametrize(
    "bad",
    ["", "A" * 32, "a" * 31, "a" * 33, "../" + "a" * 29, "a" * 30 + "/x", "z" * 32],
)
def test_create_rejects_a_nonce_that_is_not_32_lowercase_hex(probe_dir, bad):
    with pytest.raises(ProxyError):
        create_probe_file(probe_dir, bad)


def test_create_refuses_to_truncate_an_existing_file(probe_dir):
    create_probe_file(probe_dir, NONCE)
    with pytest.raises(ProxyError):
        create_probe_file(probe_dir, NONCE)


def test_events_append_in_order_and_are_never_rewritten(probe_dir, identity):
    for index in range(3):
        append_probe_event(probe_dir, identity, {"schema": 1, "event": "route", "n": index})
    events = read_probe_events(probe_dir, identity)
    assert [event["n"] for event in events] == [0, 1, 2]


def test_each_record_is_one_compact_sorted_line(probe_dir, identity):
    append_probe_event(probe_dir, identity, {"schema": 1, "b": 2, "a": 1})
    raw = Path(identity.path).read_text(encoding="utf-8")
    assert raw == '{"a":1,"b":2,"schema":1}\n'


def test_reading_an_empty_file_returns_no_events(probe_dir, identity):
    assert read_probe_events(probe_dir, identity) == ()


# --------------------------------------------------------------------------- #
# Adversarial filesystem matrix
# --------------------------------------------------------------------------- #
def test_append_rejects_a_path_outside_the_dedicated_directory(probe_dir, tmp_path):
    outside = tmp_path / "elsewhere.ndjson"
    outside.write_text("", encoding="utf-8")
    info = outside.lstat()
    identity = ProbeFileIdentity(
        path=str(outside),
        device=info.st_dev,
        inode=info.st_ino,
        uid=os.getuid(),
        mode=0o600,
    )
    with pytest.raises(ProxyError, match=SAFETY_ERROR):
        append_probe_event(probe_dir, identity, {"schema": 1})
    assert outside.read_text(encoding="utf-8") == ""


@posix_only
def test_append_rejects_a_symlinked_event_file(probe_dir, tmp_path, identity):
    target = tmp_path / "target.ndjson"
    target.write_text("", encoding="utf-8")
    Path(identity.path).unlink()
    Path(identity.path).symlink_to(target)
    with pytest.raises(ProxyError, match=SAFETY_ERROR):
        append_probe_event(probe_dir, identity, {"schema": 1})
    assert target.read_text(encoding="utf-8") == ""


@posix_only
def test_append_rejects_a_symlinked_probe_directory(tmp_path):
    real = ensure_dir(tmp_path / "real", 0o700)
    identity = create_probe_file(real, NONCE)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(ProxyError, match=SAFETY_ERROR):
        append_probe_event(link, identity, {"schema": 1})


@posix_only
def test_append_rejects_a_world_readable_event_file(probe_dir, identity):
    Path(identity.path).chmod(0o644)
    with pytest.raises(ProxyError, match=SAFETY_ERROR):
        append_probe_event(probe_dir, identity, {"schema": 1})


@posix_only
def test_append_rejects_a_loose_probe_directory(probe_dir, identity):
    probe_dir.chmod(0o755)
    try:
        with pytest.raises(ProxyError, match=SAFETY_ERROR):
            append_probe_event(probe_dir, identity, {"schema": 1})
    finally:
        probe_dir.chmod(0o700)


def test_append_rejects_a_non_regular_target(probe_dir, identity):
    Path(identity.path).unlink()
    ensure_dir(Path(identity.path), 0o700)
    with pytest.raises(ProxyError, match=SAFETY_ERROR):
        append_probe_event(probe_dir, identity, {"schema": 1})


def test_append_rejects_a_replaced_file_with_a_new_inode(probe_dir, identity):
    # The classic swap: same path, different file.
    Path(identity.path).unlink()
    replacement = probe_dir / f"{NONCE}.ndjson"
    replacement.touch(mode=0o600)
    with pytest.raises(ProxyError, match=SAFETY_ERROR):
        append_probe_event(probe_dir, identity, {"schema": 1})


def test_append_rejects_a_mismatched_recorded_uid(probe_dir, identity):
    foreign = ProbeFileIdentity(
        path=identity.path,
        device=identity.device,
        inode=identity.inode,
        uid=identity.uid + 1,
        mode=identity.mode,
    )
    with pytest.raises(ProxyError, match=SAFETY_ERROR):
        append_probe_event(probe_dir, foreign, {"schema": 1})


def test_append_rejects_a_record_at_or_above_pipe_buf(probe_dir, identity):
    limit = os.pathconf(identity.path, "PC_PIPE_BUF")
    with pytest.raises(ProxyError, match=SAFETY_ERROR):
        append_probe_event(probe_dir, identity, {"schema": 1, "pad": "x" * limit})
    assert read_probe_events(probe_dir, identity) == ()


@pytest.mark.parametrize(
    "event",
    [{"bad": {1, 2}}, {"bad": object()}, [1, 2], "scalar", None],
    ids=["set", "object", "list", "string", "none"],
)
def test_append_rejects_an_unserializable_or_non_mapping_event(probe_dir, identity, event):
    with pytest.raises(ProxyError, match=SAFETY_ERROR):
        append_probe_event(probe_dir, identity, event)
    assert read_probe_events(probe_dir, identity) == ()


def test_safety_failures_never_echo_the_event(probe_dir, tmp_path):
    canary = "sk-canary-do-not-leak"
    outside = tmp_path / "elsewhere.ndjson"
    outside.touch(mode=0o600)
    info = outside.lstat()
    identity = ProbeFileIdentity(
        path=str(outside),
        device=info.st_dev,
        inode=info.st_ino,
        uid=os.getuid(),
        mode=0o600,
    )
    with pytest.raises(ProxyError) as excinfo:
        append_probe_event(probe_dir, identity, {"schema": 1, "secret": canary})
    rendered = f"{excinfo.value.message} {excinfo.value.hint or ''}"
    assert canary not in rendered
    assert str(outside) not in rendered


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "content",
    ['{"a":1}\nnot json\n', "\n", '"scalar"\n', "[1,2]\n"],
    ids=["malformed", "blank", "scalar", "list"],
)
def test_read_rejects_a_corrupt_event_file(probe_dir, identity, content):
    Path(identity.path).write_text(content, encoding="utf-8")
    with pytest.raises(ProxyError, match="probe event file is invalid."):
        read_probe_events(probe_dir, identity)


def test_read_caps_the_file_size(probe_dir, identity):
    Path(identity.path).write_text(json.dumps({"pad": "x" * 70_000}) + "\n", encoding="utf-8")
    with pytest.raises(ProxyError):
        read_probe_events(probe_dir, identity)


def test_read_validates_the_same_identity_as_append(probe_dir, identity):
    Path(identity.path).unlink()
    (probe_dir / f"{NONCE}.ndjson").touch(mode=0o600)
    with pytest.raises(ProxyError, match=SAFETY_ERROR):
        read_probe_events(probe_dir, identity)


# --------------------------------------------------------------------------- #
# Validation and cleanup
# --------------------------------------------------------------------------- #
def test_validate_returns_nothing_and_leaks_no_descriptor(probe_dir, identity):
    assert validate_probe_file(probe_dir, identity) is None


def test_remove_deletes_only_the_still_matching_file(probe_dir, identity):
    remove_probe_file(probe_dir, identity)
    assert not Path(identity.path).exists()


def test_remove_is_idempotent(probe_dir, identity):
    remove_probe_file(probe_dir, identity)
    remove_probe_file(probe_dir, identity)


def test_remove_leaves_a_replaced_file_untouched(probe_dir, identity):
    Path(identity.path).unlink()
    replacement = probe_dir / f"{NONCE}.ndjson"
    replacement.write_text("someone else's file\n", encoding="utf-8")
    with pytest.raises(ProxyError, match=SAFETY_ERROR):
        remove_probe_file(probe_dir, identity)
    assert replacement.exists()
