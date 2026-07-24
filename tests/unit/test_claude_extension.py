"""Tests for the zero-model-call Claude Code usage UI bridge."""

from __future__ import annotations

import io
import json
import queue
import select
import threading
import time
from typing import TextIO, cast

import agent_gateway.claude_extension as extension
from agent_gateway.claude_extension import ClaudeExtensionServer
from agent_gateway.paths import get_paths


def _report() -> dict[str, object]:
    return {
        "claude": {
            "status": "ok",
            "windows": [{"label": "5 hour", "used_percent": 25}],
        },
        "codex": {
            "status": "ok",
            "plan_type": "plus",
            "buckets": [
                {
                    "label": "Codex",
                    "windows": [{"label": "1w", "used_percent": 40}],
                }
            ],
        },
    }


def _messages(*messages: dict[str, object]) -> io.StringIO:
    return io.StringIO("".join(json.dumps(message) + "\n" for message in messages))


class _BlockingReader:
    def __init__(self) -> None:
        self.lines: queue.Queue[str | None] = queue.Queue()

    def __iter__(self):
        return self

    def __next__(self) -> str:
        line = self.lines.get()
        if line is None:
            raise StopIteration
        return line

    def feed(self, message: dict[str, object]) -> None:
        self.lines.put(json.dumps(message) + "\n")

    def close(self) -> None:
        self.lines.put(None)


def _wait_for_status(paths, session_id: str, status: str) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if extension._read_ui_state(paths, session_id).get("status") == status:
            return
        time.sleep(0.01)
    raise AssertionError(f"{session_id} did not reach {status}")


def _pending(paths, session_id: str, token: str = "test-token") -> None:
    with extension._ui_lock(paths, session_id):
        extension._write_ui_state(
            paths,
            session_id,
            token=token,
            status="pending",
        )


def test_mcp_tool_opens_native_elicitation_and_blocks_model_call(tmp_path) -> None:
    paths = get_paths({"HOME": str(tmp_path)})
    _pending(paths, "native-session")
    reader = _messages(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"capabilities": {"elicitation": {}}},
        },
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "show_usage",
                "arguments": {"session_id": "native-session"},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": "agw-1",
            "result": {"action": "accept", "content": {"action": "close"}},
        },
    )
    writer = io.StringIO()
    server = ClaudeExtensionServer(
        reader,
        writer,
        paths,
        report_builder=lambda _paths: _report(),
    )

    server.run()

    output = [json.loads(line) for line in writer.getvalue().splitlines()]
    elicitation = output[2]
    assert elicitation["method"] == "elicitation/create"
    assert "Agent Gateway Usage" in elicitation["params"]["message"]
    assert "Claude Code" in elicitation["params"]["message"]
    assert "Codex · Plus" in elicitation["params"]["message"]

    tool_result = output[3]["result"]
    hook_result = json.loads(tool_result["content"][0]["text"])
    assert hook_result == {
        "decision": "block",
        "reason": "",
        "suppressOutput": True,
    }


def test_native_dialog_can_refresh_without_a_model_call(tmp_path) -> None:
    paths = get_paths({"HOME": str(tmp_path)})
    _pending(paths, "refresh-session")
    reader = _messages(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"capabilities": {"elicitation": {}}},
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "show_usage",
                "arguments": {"session_id": "refresh-session"},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": "agw-1",
            "result": {"action": "accept", "content": {"action": "refresh"}},
        },
        {
            "jsonrpc": "2.0",
            "id": "agw-2",
            "result": {"action": "cancel"},
        },
    )
    writer = io.StringIO()
    calls = 0

    def build(_paths):
        nonlocal calls
        calls += 1
        return _report()

    ClaudeExtensionServer(
        reader,
        writer,
        paths,
        report_builder=build,
    ).run()

    output = [json.loads(line) for line in writer.getvalue().splitlines()]
    assert calls == 2
    assert [message.get("method") for message in output].count("elicitation/create") == 2
    assert json.loads(output[-1]["result"]["content"][0]["text"])["decision"] == "block"


def test_guard_blocks_and_shows_terminal_fallback_when_mcp_is_unavailable(
    tmp_path, monkeypatch
) -> None:
    paths = get_paths({"HOME": str(tmp_path)})
    shown: list[dict[str, object]] = []

    def show(report: dict[str, object], *, wait_seconds: float) -> bool:
        assert wait_seconds > 0
        shown.append(report)
        return True

    monkeypatch.setattr(extension, "_show_terminal_fallback", show)

    result = extension.run_usage_guard(
        json.dumps({"session_id": "session-without-mcp"}),
        paths,
        report_builder=lambda _paths: _report(),
        wait_seconds=0,
    )

    assert json.loads(result)["decision"] == "block"
    assert shown == [_report()]


def test_terminal_fallback_timeout_still_blocks_model_call(
    tmp_path, monkeypatch
) -> None:
    paths = get_paths({"HOME": str(tmp_path)})

    class Terminal(io.StringIO):
        def close(self) -> None:
            pass

    terminal = Terminal()

    monkeypatch.setattr(extension, "open", lambda *_args, **_kwargs: terminal, raising=False)
    monkeypatch.setattr(
        select,
        "select",
        lambda *_args, **_kwargs: ([], [], []),
    )

    result = extension.run_usage_guard(
        json.dumps({"session_id": "terminal-timeout"}),
        paths,
        report_builder=lambda _paths: _report(),
        wait_seconds=0,
    )

    assert json.loads(result)["decision"] == "block"
    assert "Press Enter or q" in terminal.getvalue()


def test_guard_absolute_deadline_includes_collection_and_terminal_wait(
    tmp_path, monkeypatch
) -> None:
    paths = get_paths({"HOME": str(tmp_path)})

    class Terminal(io.StringIO):
        def close(self) -> None:
            pass

    terminal = Terminal()

    def slow_report(_paths):
        time.sleep(0.03)
        return _report()

    def no_input(_readers, _writers, _errors, timeout):
        time.sleep(timeout)
        return [], [], []

    monkeypatch.setattr(extension, "open", lambda *_args, **_kwargs: terminal, raising=False)
    monkeypatch.setattr(select, "select", no_input)
    started = time.monotonic()
    result = extension.run_usage_guard(
        json.dumps({"session_id": "absolute-timeout"}),
        paths,
        report_builder=slow_report,
        wait_seconds=0,
        total_wait_seconds=0.08,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.2
    assert json.loads(result)["decision"] == "block"
    assert "Press Enter or q" in terminal.getvalue()


def test_guard_waits_for_native_dialog_to_close_before_blocking(
    tmp_path, monkeypatch
) -> None:
    paths = get_paths({"HOME": str(tmp_path)})
    fallback_called = threading.Event()

    def unexpected_report(_paths):
        fallback_called.set()
        return _report()

    results: list[str] = []
    guard = threading.Thread(
        target=lambda: results.append(
            extension.run_usage_guard(
                json.dumps({"session_id": "connected-session"}),
                paths,
                report_builder=unexpected_report,
                wait_seconds=0.5,
            )
        )
    )
    guard.start()
    token = extension._claim_native_ui(paths, "connected-session")
    assert token is not None
    with extension._ui_lock(paths, "connected-session"):
        extension._write_ui_state(
            paths,
            "connected-session",
            token=token,
            status="native_ready",
        )
    assert guard.is_alive()
    extension._set_native_done(paths, "connected-session", token)
    guard.join(timeout=2)

    assert not guard.is_alive()
    assert json.loads(results[0])["decision"] == "block"
    assert not fallback_called.is_set()


def test_native_dialog_can_reopen_after_close_in_same_session(tmp_path) -> None:
    paths = get_paths({"HOME": str(tmp_path)})
    writer = io.StringIO()
    server = ClaudeExtensionServer(
        _messages(
            {
                "jsonrpc": "2.0",
                "id": "agw-1",
                "result": {"action": "accept", "content": {"action": "close"}},
            },
            {
                "jsonrpc": "2.0",
                "id": "agw-2",
                "result": {"action": "accept", "content": {"action": "close"}},
            },
        ),
        writer,
        paths,
        report_builder=lambda _paths: _report(),
    )
    server._client_supports_elicitation = True

    _pending(paths, "repeat-session", token="first")
    server._show_usage(1, "repeat-session")
    assert extension._read_ui_state(paths, "repeat-session")["status"] == "native_done"

    _pending(paths, "repeat-session", token="second")
    server._show_usage(2, "repeat-session")

    output = [json.loads(line) for line in writer.getvalue().splitlines()]
    assert [message.get("method") for message in output].count("elicitation/create") == 2
    assert extension._read_ui_state(paths, "repeat-session")["status"] == "native_done"


def test_expired_dialog_releases_server_and_can_reopen(tmp_path) -> None:
    paths = get_paths({"HOME": str(tmp_path)})
    blocking_reader = _BlockingReader()
    writer = io.StringIO()
    server = ClaudeExtensionServer(
        cast(TextIO, blocking_reader),
        writer,
        paths,
        report_builder=lambda _paths: _report(),
    )
    server._client_supports_elicitation = True
    results: list[str] = []
    guard = threading.Thread(
        target=lambda: results.append(
            extension.run_usage_guard(
                json.dumps({"session_id": "orphaned-dialog"}),
                paths,
                report_builder=lambda _paths: _report(),
                wait_seconds=0.5,
                dialog_wait_seconds=0.05,
            )
        )
    )
    guard.start()
    first_call = threading.Thread(
        target=lambda: server._show_usage(1, "orphaned-dialog")
    )
    first_call.start()
    _wait_for_status(paths, "orphaned-dialog", "native_aborted")
    guard.join(timeout=2)
    first_call.join(timeout=2)

    assert not guard.is_alive()
    assert not first_call.is_alive()
    parsed = json.loads(results[0])
    assert parsed["decision"] == "block"
    assert "unresponsive usage dialog" in parsed["reason"]
    assert extension._read_ui_state(paths, "orphaned-dialog")["status"] == "native_aborted"

    second_results: list[str] = []
    second_guard = threading.Thread(
        target=lambda: second_results.append(
            extension.run_usage_guard(
                json.dumps({"session_id": "orphaned-dialog"}),
                paths,
                report_builder=lambda _paths: _report(),
                wait_seconds=0.5,
                dialog_wait_seconds=1,
            )
        )
    )
    second_guard.start()
    second_call = threading.Thread(
        target=lambda: server._show_usage(2, "orphaned-dialog")
    )
    second_call.start()
    _wait_for_status(paths, "orphaned-dialog", "native_ready")
    blocking_reader.feed(
        {
            "jsonrpc": "2.0",
            "id": "agw-2",
            "result": {"action": "accept", "content": {"action": "close"}},
        }
    )
    second_call.join(timeout=2)
    second_guard.join(timeout=2)
    blocking_reader.close()

    assert not second_call.is_alive()
    assert not second_guard.is_alive()
    assert json.loads(second_results[0])["decision"] == "block"
    assert extension._read_ui_state(paths, "orphaned-dialog")["status"] == "native_done"
    output = [json.loads(line) for line in writer.getvalue().splitlines()]
    assert [message.get("method") for message in output].count("elicitation/create") == 2


def test_guard_bounds_expired_dialog_when_mcp_never_acknowledges(
    tmp_path, monkeypatch
) -> None:
    paths = get_paths({"HOME": str(tmp_path)})
    monkeypatch.setattr(extension, "_NATIVE_ABORT_ACK_WAIT_SECONDS", 0.05)
    results: list[str] = []
    guard = threading.Thread(
        target=lambda: results.append(
            extension.run_usage_guard(
                json.dumps({"session_id": "dead-mcp"}),
                paths,
                report_builder=lambda _paths: _report(),
                wait_seconds=0.5,
                dialog_wait_seconds=0.05,
            )
        )
    )
    guard.start()
    token = extension._claim_native_ui(paths, "dead-mcp")
    assert token is not None
    with extension._ui_lock(paths, "dead-mcp"):
        extension._write_ui_state(
            paths,
            "dead-mcp",
            token=token,
            status="native_ready",
        )
    guard.join(timeout=2)

    assert not guard.is_alive()
    parsed = json.loads(results[0])
    assert parsed["decision"] == "block"
    assert "unresponsive usage dialog" in parsed["reason"]
    assert extension._read_ui_state(paths, "dead-mcp")["status"] == "native_expired"


def test_server_terminates_if_stdin_closes_during_dialog(tmp_path) -> None:
    paths = get_paths({"HOME": str(tmp_path)})
    _pending(paths, "closing-session")
    blocking_reader = _BlockingReader()
    writer = io.StringIO()
    server = ClaudeExtensionServer(
        cast(TextIO, blocking_reader),
        writer,
        paths,
        report_builder=lambda _paths: _report(),
    )
    server_thread = threading.Thread(target=server.run)
    server_thread.start()
    blocking_reader.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"capabilities": {"elicitation": {}}},
        }
    )
    blocking_reader.feed(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "show_usage",
                "arguments": {"session_id": "closing-session"},
            },
        }
    )
    _wait_for_status(paths, "closing-session", "native_ready")
    blocking_reader.close()
    server_thread.join(timeout=2)

    assert not server_thread.is_alive()
    output = [json.loads(line) for line in writer.getvalue().splitlines()]
    assert output[-1]["id"] == 2


def test_delayed_mcp_cannot_open_after_fallback_claims_ui(
    tmp_path, monkeypatch
) -> None:
    paths = get_paths({"HOME": str(tmp_path)})
    fallback_started = threading.Event()
    release_fallback = threading.Event()

    def show(_report: dict[str, object], *, wait_seconds: float) -> bool:
        assert wait_seconds > 0
        fallback_started.set()
        assert release_fallback.wait(timeout=2)
        return True

    monkeypatch.setattr(extension, "_show_terminal_fallback", show)
    results: list[str] = []
    guard = threading.Thread(
        target=lambda: results.append(
            extension.run_usage_guard(
                json.dumps({"session_id": "delayed-mcp"}),
                paths,
                report_builder=lambda _paths: _report(),
                wait_seconds=0,
            )
        )
    )
    guard.start()
    assert fallback_started.wait(timeout=2)

    assert extension._claim_native_ui(paths, "delayed-mcp") is None
    release_fallback.set()
    guard.join(timeout=2)

    assert not guard.is_alive()
    assert json.loads(results[0])["decision"] == "block"


def test_native_failure_transfers_ui_ownership_to_guard(
    tmp_path, monkeypatch
) -> None:
    paths = get_paths({"HOME": str(tmp_path)})
    shown: list[dict[str, object]] = []

    def show(report: dict[str, object], *, wait_seconds: float) -> bool:
        assert wait_seconds > 0
        shown.append(report)
        return True

    monkeypatch.setattr(extension, "_show_terminal_fallback", show)
    results: list[str] = []
    guard = threading.Thread(
        target=lambda: results.append(
            extension.run_usage_guard(
                json.dumps({"session_id": "native-failure"}),
                paths,
                report_builder=lambda _paths: _report(),
                wait_seconds=0.5,
            )
        )
    )
    guard.start()
    token = extension._claim_native_ui(paths, "native-failure")
    assert token is not None
    extension._set_native_failed(paths, "native-failure", token)
    guard.join(timeout=2)

    assert not guard.is_alive()
    assert shown == [_report()]
    assert json.loads(results[0])["decision"] == "block"


def test_guard_still_blocks_if_local_usage_collection_crashes(tmp_path) -> None:
    paths = get_paths({"HOME": str(tmp_path)})

    def broken_report(_paths):
        raise RuntimeError("unexpected local failure")

    result = extension.run_usage_guard(
        json.dumps({"session_id": "broken-usage"}),
        paths,
        report_builder=broken_report,
        wait_seconds=0,
    )

    parsed = json.loads(result)
    assert parsed["decision"] == "block"
    assert "could not open" in parsed["reason"]


def test_guard_still_blocks_if_coordination_storage_fails(
    tmp_path, monkeypatch
) -> None:
    paths = get_paths({"HOME": str(tmp_path)})
    shown: list[dict[str, object]] = []

    def broken_lock(_paths, _session_id):
        raise OSError("state directory unavailable")

    def show(report: dict[str, object], *, wait_seconds: float) -> bool:
        assert wait_seconds > 0
        shown.append(report)
        return True

    monkeypatch.setattr(extension, "_ui_lock", broken_lock)
    monkeypatch.setattr(extension, "_show_terminal_fallback", show)

    result = extension.run_usage_guard(
        json.dumps({"session_id": "storage-failure"}),
        paths,
        report_builder=lambda _paths: _report(),
    )

    assert json.loads(result)["decision"] == "block"
    assert shown == [_report()]


def test_guard_blocks_even_if_coordination_and_fallback_both_fail(
    tmp_path, monkeypatch
) -> None:
    paths = get_paths({"HOME": str(tmp_path)})

    monkeypatch.setattr(
        extension,
        "_ui_lock",
        lambda _paths, _session_id: (_ for _ in ()).throw(OSError("no state")),
    )

    def broken_report(_paths):
        raise OSError("no credentials")

    result = extension.run_usage_guard(
        json.dumps({"session_id": "total-failure"}),
        paths,
        report_builder=broken_report,
    )

    parsed = json.loads(result)
    assert parsed["decision"] == "block"
    assert "could not open" in parsed["reason"]
