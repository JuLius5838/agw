"""Zero-model-call Claude Code UI integration for Agent Gateway usage."""

from __future__ import annotations

import json
import os
import queue
import select
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, TextIO

from filelock import FileLock

from agent_gateway import __version__
from agent_gateway.paths import (
    SECRET_FILE_MODE,
    Paths,
    atomic_write_text,
    ensure_dir,
    get_paths,
)
from agent_gateway.usage import build_usage_report, render_usage

_DEFAULT_PROTOCOL_VERSION = "2025-06-18"
_TOOL_NAME = "show_usage"
_MAX_REFRESHES = 20
_GUARD_WAIT_SECONDS = 0.75
_NATIVE_READY_WAIT_SECONDS = 15.0
_NATIVE_DIALOG_WAIT_SECONDS = 300.0
_NATIVE_ABORT_ACK_WAIT_SECONDS = 1.0
_TERMINAL_FALLBACK_WAIT_SECONDS = 300.0
_GUARD_TOTAL_WAIT_SECONDS = 320.0
_MCP_CLAIM_WAIT_SECONDS = 0.25


def _hook_block_result(reason: str = "") -> str:
    """Return a hook decision that consumes the slash command without an LLM call."""
    return json.dumps(
        {
            "decision": "block",
            "reason": reason,
            "suppressOutput": True,
        },
        separators=(",", ":"),
    )


def _ui_state_path(paths: Paths, session_id: str) -> Path:
    digest = sha256(session_id.encode("utf-8")).hexdigest()[:24]
    return paths.usage_dir / f"claude-dialog-{digest}.json"


def _ui_lock(paths: Paths, session_id: str) -> FileLock:
    return FileLock(str(_ui_state_path(paths, session_id)) + ".lock", timeout=1)


def _read_ui_state(paths: Paths, session_id: str) -> dict[str, object]:
    try:
        document = json.loads(_ui_state_path(paths, session_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return document if isinstance(document, dict) else {}


def _write_ui_state(
    paths: Paths,
    session_id: str,
    *,
    token: str,
    status: str,
) -> None:
    ensure_dir(paths.usage_dir)
    atomic_write_text(
        _ui_state_path(paths, session_id),
        json.dumps(
            {
                "token": token,
                "status": status,
                "updated_at": time.time(),
            },
            separators=(",", ":"),
        )
        + "\n",
        mode=SECRET_FILE_MODE,
    )


def _claim_native_ui(paths: Paths, session_id: str) -> str | None:
    deadline = time.monotonic() + _MCP_CLAIM_WAIT_SECONDS
    while time.monotonic() < deadline:
        ensure_dir(paths.usage_dir)
        with _ui_lock(paths, session_id):
            state = _read_ui_state(paths, session_id)
            token = state.get("token")
            status = state.get("status")
            if isinstance(token, str) and status == "pending":
                _write_ui_state(
                    paths,
                    session_id,
                    token=token,
                    status="native_claimed",
                )
                return token
            if status in {"fallback_claimed", "native_ready"}:
                return None
        time.sleep(0.025)
    return None


def _set_native_failed(paths: Paths, session_id: str, token: str) -> None:
    ensure_dir(paths.usage_dir)
    with _ui_lock(paths, session_id):
        state = _read_ui_state(paths, session_id)
        if state.get("token") == token and state.get("status") in {
            "native_claimed",
            "native_ready",
        }:
            _write_ui_state(
                paths,
                session_id,
                token=token,
                status="native_failed",
            )


def _set_native_done(paths: Paths, session_id: str, token: str) -> None:
    ensure_dir(paths.usage_dir)
    with _ui_lock(paths, session_id):
        state = _read_ui_state(paths, session_id)
        if state.get("token") == token and state.get("status") == "native_ready":
            _write_ui_state(
                paths,
                session_id,
                token=token,
                status="native_done",
            )


def _finish_native_request(paths: Paths, session_id: str, token: str) -> None:
    """Acknowledge that the MCP tools/call response was written and is reusable."""
    ensure_dir(paths.usage_dir)
    with _ui_lock(paths, session_id):
        state = _read_ui_state(paths, session_id)
        if state.get("token") != token:
            return
        status = state.get("status")
        if status == "native_ready":
            final_status = "native_done"
        elif status == "native_expired":
            final_status = "native_aborted"
        else:
            return
        _write_ui_state(
            paths,
            session_id,
            token=token,
            status=final_status,
        )


def run_usage_guard(
    payload: str,
    paths: Paths,
    *,
    report_builder: Callable[[Paths], dict[str, object]] = build_usage_report,
    wait_seconds: float = _GUARD_WAIT_SECONDS,
    dialog_wait_seconds: float = _NATIVE_DIALOG_WAIT_SECONDS,
    total_wait_seconds: float = _GUARD_TOTAL_WAIT_SECONDS,
) -> str:
    """Fail closed and render a local fallback if the native MCP hook is unavailable."""
    overall_deadline = time.monotonic() + max(0.0, total_wait_seconds)
    try:
        return _run_usage_guard(
            payload,
            paths,
            report_builder=report_builder,
            wait_seconds=wait_seconds,
            dialog_wait_seconds=dialog_wait_seconds,
            overall_deadline=overall_deadline,
        )
    except Exception:  # noqa: BLE001 - a hook failure must never reach the model
        return _guard_fallback_result(paths, report_builder, overall_deadline)


def _run_usage_guard(
    payload: str,
    paths: Paths,
    *,
    report_builder: Callable[[Paths], dict[str, object]],
    wait_seconds: float,
    dialog_wait_seconds: float,
    overall_deadline: float,
) -> str:
    """Coordinate exclusive ownership between the native and terminal UIs."""
    try:
        document = json.loads(payload)
    except json.JSONDecodeError:
        document = {}
    session_id = document.get("session_id") if isinstance(document, dict) else None
    if not isinstance(session_id, str) or not session_id:
        session_id = "unknown-session"

    token = f"{time.time_ns()}-{os.getpid()}"
    ensure_dir(paths.usage_dir)
    with _ui_lock(paths, session_id):
        _write_ui_state(paths, session_id, token=token, status="pending")

    now = time.monotonic()
    claim_deadline = min(now + max(0.0, wait_seconds), overall_deadline)
    ready_deadline = min(now + _NATIVE_READY_WAIT_SECONDS, overall_deadline)
    dialog_deadline = min(
        now + max(0.0, dialog_wait_seconds),
        overall_deadline - _NATIVE_ABORT_ACK_WAIT_SECONDS,
    )
    abort_ack_deadline: float | None = None
    while True:
        with _ui_lock(paths, session_id):
            state = _read_ui_state(paths, session_id)
            if state.get("token") != token:
                return _hook_block_result(
                    "AGW blocked overlapping usage commands. Run /agw-usage again."
                )
            status = state.get("status")
            if status == "native_done":
                return _hook_block_result()
            if status == "native_aborted":
                return _hook_block_result(
                    "AGW closed an unresponsive usage dialog. Run /agw-usage again."
                )
            now = time.monotonic()
            if status == "native_expired":
                if abort_ack_deadline is None:
                    abort_ack_deadline = min(
                        now + _NATIVE_ABORT_ACK_WAIT_SECONDS,
                        overall_deadline,
                    )
                if now >= abort_ack_deadline:
                    return _hook_block_result(
                        "AGW closed an unresponsive usage dialog. Run /agw-usage again."
                    )
            if status == "native_ready" and now >= dialog_deadline:
                _write_ui_state(
                    paths,
                    session_id,
                    token=token,
                    status="native_expired",
                )
                abort_ack_deadline = min(
                    now + _NATIVE_ABORT_ACK_WAIT_SECONDS,
                    overall_deadline,
                )
                continue
            should_fallback = status == "native_failed"
            should_fallback = should_fallback or (
                status == "pending" and now >= claim_deadline
            )
            should_fallback = should_fallback or (
                status == "native_claimed" and now >= ready_deadline
            )
            should_fallback = should_fallback or now >= overall_deadline
            if should_fallback:
                _write_ui_state(
                    paths,
                    session_id,
                    token=token,
                    status="fallback_claimed",
                )
                break
        time.sleep(0.025)

    return _guard_fallback_result(paths, report_builder, overall_deadline)


def _guard_fallback_result(
    paths: Paths,
    report_builder: Callable[[Paths], dict[str, object]],
    overall_deadline: float,
) -> str:
    report = _build_report_before_deadline(paths, report_builder, overall_deadline)
    remaining = min(
        _TERMINAL_FALLBACK_WAIT_SECONDS,
        max(0.0, overall_deadline - time.monotonic()),
    )
    try:
        shown = (
            report is not None
            and remaining > 0
            and _show_terminal_fallback(report, wait_seconds=remaining)
        )
    except Exception:  # noqa: BLE001 - this guard must fail closed
        shown = False
    if shown:
        return _hook_block_result()
    return _hook_block_result(
        "AGW blocked this command because its local usage UI could not open. "
        "Run `agw usage` in the terminal."
    )


def _build_report_before_deadline(
    paths: Paths,
    report_builder: Callable[[Paths], dict[str, object]],
    deadline: float,
) -> dict[str, object] | None:
    """Collect provider usage without allowing a stalled collector to outlive the hook."""
    results: queue.Queue[dict[str, object] | None] = queue.Queue(maxsize=1)

    def build() -> None:
        try:
            results.put(report_builder(paths))
        except Exception:  # noqa: BLE001 - the guard reports fallback failure
            results.put(None)

    threading.Thread(
        target=build,
        name="agw-usage-report",
        daemon=True,
    ).start()
    try:
        return results.get(timeout=max(0.0, deadline - time.monotonic()))
    except queue.Empty:
        return None


def _usage_elicitation(report: dict[str, object]) -> dict[str, object]:
    """Build a native Claude Code form that doubles as a read-only usage panel."""
    return {
        "message": render_usage(report),
        "requestedSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "title": "Action",
                    "description": "Refresh provider limits or return to Claude Code.",
                    "enum": ["close", "refresh"],
                    "enumNames": ["Close", "Refresh"],
                    "default": "close",
                }
            },
            "required": ["action"],
        },
    }


@dataclass
class ClaudeExtensionServer:
    """Minimal JSON-RPC/MCP server used only by the AGW Claude plugin."""

    reader: TextIO
    writer: TextIO
    paths: Paths
    report_builder: Callable[[Paths], dict[str, object]] = build_usage_report

    def __post_init__(self) -> None:
        self._next_request_id = 1
        self._client_supports_elicitation = False
        self._protocol_version = _DEFAULT_PROTOCOL_VERSION
        self._input_lines: queue.Queue[str | None] = queue.Queue()
        self._reader_thread: threading.Thread | None = None

    def run(self) -> None:
        self._ensure_reader_thread()
        while True:
            raw_line = self._input_lines.get()
            if raw_line is None:
                break
            try:
                message = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            self._handle_message(message)

    def _ensure_reader_thread(self) -> None:
        if self._reader_thread is not None:
            return
        self._reader_thread = threading.Thread(
            target=self._read_input,
            name="agw-claude-extension-reader",
            daemon=True,
        )
        self._reader_thread.start()

    def _read_input(self) -> None:
        try:
            for raw_line in self.reader:
                self._input_lines.put(raw_line)
        finally:
            self._input_lines.put(None)

    def _handle_message(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize" and request_id is not None:
            params = message.get("params")
            if isinstance(params, dict):
                requested_version = params.get("protocolVersion")
                if isinstance(requested_version, str) and requested_version:
                    self._protocol_version = requested_version
                capabilities = params.get("capabilities")
                self._client_supports_elicitation = (
                    isinstance(capabilities, dict) and "elicitation" in capabilities
                )
            self._respond(
                request_id,
                {
                    "protocolVersion": self._protocol_version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "agent-gateway",
                        "title": "Agent Gateway",
                        "version": __version__,
                    },
                },
            )
            return
        if method == "tools/list" and request_id is not None:
            self._respond(
                request_id,
                {
                    "tools": [
                        {
                            "name": _TOOL_NAME,
                            "title": "Show Agent Gateway usage",
                            "description": (
                                "Display Claude Code and Codex subscription limits "
                                "in a native local dialog."
                            ),
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "session_id": {
                                        "type": "string",
                                        "description": "Claude Code session identifier.",
                                    }
                                },
                                "additionalProperties": False,
                            },
                            "annotations": {
                                "readOnlyHint": True,
                                "destructiveHint": False,
                                "idempotentHint": False,
                                "openWorldHint": False,
                            },
                        }
                    ]
                },
            )
            return
        if method == "tools/call" and request_id is not None:
            params = message.get("params")
            name = params.get("name") if isinstance(params, dict) else None
            if name != _TOOL_NAME:
                self._error(request_id, -32602, f"Unknown tool: {name}")
                return
            arguments = params.get("arguments") if isinstance(params, dict) else None
            session_id = arguments.get("session_id") if isinstance(arguments, dict) else None
            self._show_usage(
                request_id,
                session_id if isinstance(session_id, str) else "unknown-session",
            )

    def _show_usage(self, request_id: object, session_id: str) -> None:
        token = _claim_native_ui(self.paths, session_id)
        if token is not None and self._client_supports_elicitation:
            try:
                for _attempt in range(_MAX_REFRESHES):
                    report = self.report_builder(self.paths)
                    response = self._request_claimed(
                        "elicitation/create",
                        _usage_elicitation(report),
                        session_id=session_id,
                        token=token,
                    )
                    if not isinstance(response, dict):
                        raise RuntimeError("Claude Code ended the elicitation without a response")
                    if response.get("action") != "accept":
                        break
                    content = response.get("content")
                    action = content.get("action") if isinstance(content, dict) else None
                    if action != "refresh":
                        break
            except Exception:  # noqa: BLE001 - the parallel guard owns recovery
                _set_native_failed(self.paths, session_id, token)
        elif token is not None:
            _set_native_failed(self.paths, session_id, token)

        self._respond(
            request_id,
            {
                "content": [{"type": "text", "text": _hook_block_result()}],
                "isError": False,
            },
        )
        if token is not None:
            # Signal only after the MCP tools/call response is on stdout. If the
            # guard blocks earlier, Claude cancels this still-running hook and
            # the stdio server cannot service a second /agw-usage invocation.
            _finish_native_request(self.paths, session_id, token)

    def _request_claimed(
        self,
        method: str,
        params: dict[str, object],
        *,
        session_id: str,
        token: str,
    ) -> object:
        request_id = f"agw-{self._next_request_id}"
        self._next_request_id += 1
        ensure_dir(self.paths.usage_dir)
        with _ui_lock(self.paths, session_id):
            state = _read_ui_state(self.paths, session_id)
            if state.get("token") != token or state.get("status") not in {
                "native_claimed",
                "native_ready",
            }:
                return None
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
            _write_ui_state(
                self.paths,
                session_id,
                token=token,
                status="native_ready",
            )
        return self._read_request_response(
            request_id,
            session_id=session_id,
            token=token,
        )

    def _request(self, method: str, params: dict[str, object]) -> object:
        request_id = f"agw-{self._next_request_id}"
        self._next_request_id += 1
        self._write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        return self._read_request_response(request_id)

    def _read_request_response(
        self,
        request_id: object,
        *,
        session_id: str | None = None,
        token: str | None = None,
    ) -> object:
        self._ensure_reader_thread()
        while True:
            try:
                raw_line = self._input_lines.get(timeout=0.05)
            except queue.Empty:
                if session_id is not None and token is not None:
                    state = _read_ui_state(self.paths, session_id)
                    if (
                        state.get("token") == token
                        and state.get("status") == "native_expired"
                    ):
                        raise RuntimeError("usage dialog expired") from None
                continue
            if raw_line is None:
                # The nested elicitation reader and the outer server loop share
                # this queue. Preserve EOF so the outer loop can terminate too.
                self._input_lines.put(None)
                return None
            try:
                message = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("id") == request_id and "method" not in message:
                return message.get("result")
            self._handle_message(message)

    def _respond(self, request_id: object, result: object) -> None:
        self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _error(self, request_id: object, code: int, message: str) -> None:
        self._write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": code, "message": message},
            }
        )

    def _write(self, message: dict[str, object]) -> None:
        self.writer.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.writer.flush()


def _show_terminal_fallback(
    report: dict[str, object],
    *,
    wait_seconds: float = _TERMINAL_FALLBACK_WAIT_SECONDS,
) -> bool:
    """Show a same-terminal fallback when the client lacks native elicitation."""
    if os.name != "posix" or wait_seconds <= 0:
        return False
    try:
        with open("/dev/tty", "r+", encoding="utf-8", buffering=1) as terminal:
            terminal.write("\033[2J\033[H")
            terminal.write(render_usage(report))
            terminal.write("\n\nPress Enter or q to return to Claude Code.")
            terminal.flush()
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline:
                remaining = max(0.0, deadline - time.monotonic())
                readable, _, _ = select.select([terminal], [], [], remaining)
                if not readable:
                    break
                character = terminal.read(1)
                if character in {"", "\n", "\r", "q", "Q", "\x1b"}:
                    break
            terminal.write("\033[2J\033[H")
            terminal.flush()
        return True
    except OSError:
        return False


def main() -> None:
    """Run the plugin MCP server over stdio."""
    ClaudeExtensionServer(sys.stdin, sys.stdout, get_paths()).run()
