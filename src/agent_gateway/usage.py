"""Unified Claude Code and ChatGPT/Codex subscription usage.

Claude Code exposes subscription windows to status-line commands.  AGW captures
only those documented fields (never the rest of the session payload) into a
small local snapshot.  ChatGPT/Codex limits are queried on demand through the
official Codex App Server account RPCs using AGW's existing ChatGPT credential.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import math
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agent_gateway import __version__
from agent_gateway.config import executable_sha256, load_config
from agent_gateway.errors import ConfigError
from agent_gateway.paths import (
    SECRET_FILE_MODE,
    Paths,
    atomic_write_text,
    ensure_dir,
    read_text,
)
from agent_gateway.redaction import redact

_CLAUDE_WINDOWS = (
    ("five_hour", "5 hour"),
    ("seven_day", "7 day"),
)
_APP_SERVER_TIMEOUT_SECONDS = 12.0
_TOKEN_EXPIRY_SKEW_SECONDS = 60.0
_SUPPORTED_CODEX_SERIES = (0, 145)
_CODEX_VERSION = re.compile(r"\bcodex(?:-cli)?\s+(\d+)\.(\d+)\.(\d+)\b", re.IGNORECASE)
_APP_SERVER_ENV_KEYS = {
    "ALL_PROXY",
    "HOME",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "NO_PROXY",
    "PATH",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TMPDIR",
    "TZ",
    "USER",
    "all_proxy",
    "https_proxy",
    "http_proxy",
    "no_proxy",
}


class UsageUnavailable(Exception):
    """A usage source cannot currently provide data."""


def capture_claude_status(paths: Paths, payload: str) -> bool:
    """Persist only Claude's documented rate-limit fields from status-line JSON.

    Returns ``True`` when a usable snapshot was written. Missing rate-limit data
    is normal before Claude's first API response and leaves the previous snapshot
    intact.
    """
    try:
        document = json.loads(payload)
    except json.JSONDecodeError:
        return False
    if not isinstance(document, dict):
        return False
    raw_limits = document.get("rate_limits")
    if not isinstance(raw_limits, dict):
        return False

    limits: dict[str, dict[str, float]] = {}
    for key, _label in _CLAUDE_WINDOWS:
        raw_window = raw_limits.get(key)
        if not isinstance(raw_window, dict):
            continue
        used = _number(raw_window.get("used_percentage"))
        resets_at = _timestamp(raw_window.get("resets_at"))
        if used is None:
            continue
        used = max(0.0, min(100.0, used))
        window: dict[str, float] = {"used_percentage": used}
        if resets_at is not None:
            window["resets_at"] = resets_at
        limits[key] = window

    if not limits:
        return False
    snapshot = {
        "captured_at": time.time(),
        "rate_limits": limits,
    }
    ensure_dir(paths.usage_dir)
    atomic_write_text(
        paths.claude_usage_file,
        json.dumps(snapshot, sort_keys=True) + "\n",
        mode=SECRET_FILE_MODE,
    )
    return True


def read_claude_usage(paths: Paths) -> dict[str, object]:
    """Return a normalized Claude usage source document."""
    if not paths.claude_usage_file.is_file():
        return {
            "status": "unavailable",
            "message": "complete one Claude response after restarting Claude Code",
        }
    try:
        snapshot = json.loads(read_text(paths.claude_usage_file))
    except (OSError, json.JSONDecodeError):
        return {
            "status": "unavailable",
            "message": "Claude usage snapshot is unreadable; restart Claude Code",
        }
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("rate_limits"), dict):
        return {
            "status": "unavailable",
            "message": "Claude usage snapshot is invalid; restart Claude Code",
        }

    windows: list[dict[str, object]] = []
    limits = snapshot["rate_limits"]
    assert isinstance(limits, dict)
    for key, label in _CLAUDE_WINDOWS:
        raw_window = limits.get(key)
        if not isinstance(raw_window, dict):
            continue
        used = _number(raw_window.get("used_percentage"))
        if used is None:
            continue
        used = max(0.0, min(100.0, used))
        window: dict[str, object] = {
            "id": key,
            "label": label,
            "used_percent": used,
        }
        resets_at = _timestamp(raw_window.get("resets_at"))
        if resets_at is not None:
            window["resets_at"] = resets_at
        windows.append(window)

    if not windows:
        return {
            "status": "unavailable",
            "message": "Claude has not reported subscription windows yet",
        }
    return {
        "status": "ok",
        "captured_at": _timestamp(snapshot.get("captured_at")),
        "windows": windows,
    }


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    return None


def _timestamp(value: object) -> float | None:
    numeric = _number(value)
    if numeric is not None:
        return numeric
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _safe_text(value: object, *, max_length: int = 120) -> str:
    """Return printable, redacted provider text suitable for JSON and terminals."""
    printable = "".join(character for character in str(value) if character.isprintable())
    return redact(printable)[:max_length]


def _jwt_claims(token: str) -> dict[str, object]:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _account_id(document: dict[str, object]) -> str | None:
    existing = document.get("account_id")
    if isinstance(existing, str) and existing:
        return existing
    for key in ("id_token", "access_token"):
        token = document.get(key)
        if not isinstance(token, str):
            continue
        auth = _jwt_claims(token).get("https://api.openai.com/auth")
        if isinstance(auth, dict):
            value = auth.get("chatgpt_account_id")
            if isinstance(value, str) and value:
                return value
    return None


def _load_chatgpt_credentials(paths: Paths) -> tuple[str, str]:
    auth_file = paths.provider_credentials_dir("chatgpt") / "auth.json"
    if not auth_file.is_file():
        raise UsageUnavailable("run `agw auth chatgpt`")
    try:
        document = json.loads(read_text(auth_file))
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageUnavailable(
            "ChatGPT credential is unreadable; run `agw auth chatgpt --force`"
        ) from exc
    if not isinstance(document, dict):
        raise UsageUnavailable("ChatGPT credential is invalid; run `agw auth chatgpt --force`")

    access_token = document.get("access_token")
    expires_at = _timestamp(document.get("expires_at"))
    if not isinstance(access_token, str) or not access_token:
        raise UsageUnavailable(
            "ChatGPT credential has no access token; run `agw auth chatgpt --force`"
        )

    if expires_at is not None and time.time() >= expires_at - _TOKEN_EXPIRY_SKEW_SECONDS:
        raise UsageUnavailable(
            "ChatGPT credential needs refresh; use a Codex model once and retry, "
            "or run `agw auth chatgpt --force`"
        )

    account_id = _account_id(document)
    if account_id is None:
        raise UsageUnavailable("ChatGPT account id is missing; run `agw auth chatgpt --force`")
    return access_token, account_id


def _require_supported_codex(
    executable: str,
    *,
    env: dict[str, str] | None = None,
) -> None:
    """Reject unverified Codex versions before sending an OAuth credential."""
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UsageUnavailable("could not determine the Codex CLI version") from exc
    match = _CODEX_VERSION.search(f"{result.stdout}\n{result.stderr}")
    if result.returncode != 0 or match is None:
        raise UsageUnavailable("could not determine the Codex CLI version")
    series = (int(match.group(1)), int(match.group(2)))
    if series != _SUPPORTED_CODEX_SERIES:
        actual = ".".join(match.groups())
        expected = ".".join(str(value) for value in _SUPPORTED_CODEX_SERIES) + ".x"
        raise UsageUnavailable(
            f"Codex CLI {actual} is unverified for AGW usage; use {expected} or update AGW"
        )


def _app_server_env(codex_home: str) -> dict[str, str]:
    """Build a minimal child environment without Claude/AGW/provider secrets."""
    env = {key: value for key, value in os.environ.items() if key in _APP_SERVER_ENV_KEYS}
    env["CODEX_HOME"] = codex_home
    return env


@dataclass
class _AppServer:
    """Small JSONL client for the account-only Codex App Server RPC surface."""

    executable: str
    env: dict[str, str]
    timeout: float = _APP_SERVER_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        self._next_id = 1
        self._process: subprocess.Popen[str] | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._reader: threading.Thread | None = None

    def __enter__(self) -> _AppServer:
        self._process = subprocess.Popen(
            [self.executable, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=self.env,
        )
        if self._process.stdout is None:
            raise UsageUnavailable("Codex App Server did not expose stdout")
        self._reader = threading.Thread(
            target=self._read_stdout,
            name="agw-codex-app-server-reader",
            daemon=True,
        )
        self._reader.start()
        return self

    def __exit__(self, *_args: object) -> None:
        process = self._process
        if process is None:
            return
        try:
            if process.stdin is not None:
                with contextlib.suppress(OSError):
                    process.stdin.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        finally:
            if process.stdout is not None:
                with contextlib.suppress(OSError):
                    process.stdout.close()
            if self._reader is not None:
                self._reader.join(timeout=2)

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            self._lines.put(None)
            return
        try:
            for line in process.stdout:
                self._lines.put(line)
        finally:
            self._lines.put(None)

    def notify(self, method: str, params: dict[str, object] | None = None) -> None:
        message: dict[str, object] = {"method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def request(
        self,
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        request_id = self._next_id
        self._next_id += 1
        message: dict[str, object] = {"method": method, "id": request_id}
        if params is not None:
            message["params"] = params
        self._send(message)
        return self._read_response(request_id)

    def _send(self, message: dict[str, object]) -> None:
        if self._process is None or self._process.stdin is None:
            raise UsageUnavailable("Codex App Server is not running")
        try:
            self._process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise UsageUnavailable("Codex App Server closed unexpectedly") from exc

    def _read_response(self, request_id: int) -> dict[str, object]:
        if self._process is None:
            raise UsageUnavailable("Codex App Server is not running")
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UsageUnavailable("Codex App Server timed out")
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise UsageUnavailable("Codex App Server timed out") from exc
            if line is None:
                raise UsageUnavailable("Codex App Server exited before returning usage")
            if not line:
                raise UsageUnavailable("Codex App Server timed out")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                not isinstance(message, dict)
                or "method" in message
                or message.get("id") != request_id
            ):
                continue
            error = message.get("error")
            if isinstance(error, dict):
                raw_message = error.get("message")
                detail = _safe_text(raw_message) if raw_message else "request rejected"
                raise UsageUnavailable(f"Codex App Server: {detail}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise UsageUnavailable("Codex App Server returned an invalid response")
            return result


def _normalize_codex_usage(
    rate_result: dict[str, object],
    activity_result: dict[str, object] | None,
) -> dict[str, object]:
    buckets_raw = rate_result.get("rateLimitsByLimitId")
    if not isinstance(buckets_raw, dict) or not buckets_raw:
        single = rate_result.get("rateLimits")
        buckets_raw = {"codex": single} if isinstance(single, dict) else {}

    buckets: list[dict[str, object]] = []
    plan_type: str | None = None
    credits: dict[str, object] | None = None
    for bucket_id, raw_bucket in buckets_raw.items():
        if not isinstance(raw_bucket, dict):
            continue
        label_raw = raw_bucket.get("limitName")
        label = _safe_text(
            label_raw
            if isinstance(label_raw, str) and label_raw
            else str(bucket_id).replace("_", " ").title()
        )
        bucket: dict[str, object] = {
            "id": _safe_text(raw_bucket.get("limitId") or bucket_id),
            "label": label,
            "windows": [],
        }
        windows: list[dict[str, object]] = []
        for window_kind in ("primary", "secondary"):
            raw_window = raw_bucket.get(window_kind)
            if not isinstance(raw_window, dict):
                continue
            used = _number(raw_window.get("usedPercent"))
            duration = _number(raw_window.get("windowDurationMins"))
            if used is None:
                continue
            used = max(0.0, min(100.0, used))
            window: dict[str, object] = {
                "kind": window_kind,
                "used_percent": used,
            }
            if duration is not None:
                window["window_duration_mins"] = int(duration)
                window["label"] = _duration_label(int(duration))
            resets_at = _timestamp(raw_window.get("resetsAt"))
            if resets_at is not None:
                window["resets_at"] = resets_at
            windows.append(window)
        bucket["windows"] = windows
        reached = raw_bucket.get("rateLimitReachedType")
        if isinstance(reached, str) and reached:
            bucket["reached_type"] = _safe_text(reached)
        raw_plan = raw_bucket.get("planType")
        if plan_type is None and isinstance(raw_plan, str) and raw_plan:
            plan_type = _safe_text(raw_plan)
        raw_credits = raw_bucket.get("credits")
        if credits is None and isinstance(raw_credits, dict):
            clean_credits: dict[str, object] = {}
            for key in ("hasCredits", "unlimited"):
                value = raw_credits.get(key)
                if isinstance(value, bool):
                    clean_credits[key] = value
            balance = raw_credits.get("balance")
            if isinstance(balance, str | int | float) and not isinstance(balance, bool):
                clean_credits["balance"] = _safe_text(balance)
            if clean_credits:
                credits = clean_credits
        if windows:
            buckets.append(bucket)

    normalized: dict[str, object] = {
        "status": "ok",
        "buckets": buckets,
    }
    if plan_type:
        normalized["plan_type"] = plan_type
    if credits:
        normalized["credits"] = credits
    reset_credits = rate_result.get("rateLimitResetCredits")
    if isinstance(reset_credits, dict):
        count = reset_credits.get("availableCount")
        if isinstance(count, int):
            normalized["reset_credits_available"] = count

    if activity_result is not None:
        summary = activity_result.get("summary")
        if isinstance(summary, dict):
            clean_summary: dict[str, int] = {}
            for key in (
                "lifetimeTokens",
                "peakDailyTokens",
                "longestRunningTurnSec",
                "currentStreakDays",
                "longestStreakDays",
            ):
                value = summary.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    clean_summary[key] = value
            if clean_summary:
                normalized["activity_summary"] = clean_summary
        daily = activity_result.get("dailyUsageBuckets")
        if isinstance(daily, list):
            clean_daily: list[dict[str, object]] = []
            for raw_day in daily:
                if not isinstance(raw_day, dict):
                    continue
                start_date = raw_day.get("startDate")
                tokens = raw_day.get("tokens")
                if (
                    isinstance(start_date, str)
                    and isinstance(tokens, int)
                    and not isinstance(tokens, bool)
                ):
                    clean_daily.append(
                        {
                            "startDate": _safe_text(start_date, max_length=32),
                            "tokens": tokens,
                        }
                    )
            if clean_daily:
                normalized["daily_usage_buckets"] = clean_daily
    return normalized


def read_codex_usage(paths: Paths) -> dict[str, object]:
    """Read Codex limits and token activity through an isolated App Server."""
    try:
        executable = _trusted_codex_executable(paths)
        access_token, account_id = _load_chatgpt_credentials(paths)
        with tempfile.TemporaryDirectory(prefix="agw-codex-usage-") as temp_dir:
            os.chmod(temp_dir, 0o700)
            env = _app_server_env(temp_dir)
            _require_supported_codex(executable, env=env)
            with _AppServer(executable, env) as server:
                server.request(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "agent_gateway",
                            "title": "Agent Gateway",
                            "version": __version__,
                        },
                        "capabilities": {"experimentalApi": True},
                    },
                )
                server.notify("initialized", {})
                server.request(
                    "account/login/start",
                    {
                        "type": "chatgptAuthTokens",
                        "accessToken": access_token,
                        "chatgptAccountId": account_id,
                    },
                )
                rate_result = server.request("account/rateLimits/read")
                try:
                    activity_result = server.request("account/usage/read")
                except UsageUnavailable:
                    activity_result = None
        return _normalize_codex_usage(rate_result, activity_result)
    except UsageUnavailable as exc:
        return {"status": "unavailable", "message": str(exc)}
    except (OSError, subprocess.SubprocessError):
        return {
            "status": "unavailable",
            "message": "could not launch Codex App Server",
        }


def _trusted_codex_executable(paths: Paths) -> str:
    """Verify the setup-pinned Codex executable before sharing an OAuth token."""
    try:
        config = load_config(paths)
    except ConfigError as exc:
        raise UsageUnavailable("run `agw setup` to trust the Codex CLI") from exc
    if not config.codex_cli_path or not config.codex_cli_sha256:
        raise UsageUnavailable("run `agw setup` to trust the Codex CLI")
    candidate = Path(config.codex_cli_path)
    if not candidate.is_absolute():
        raise UsageUnavailable("Codex CLI trust record is invalid; run `agw setup`")
    try:
        resolved = candidate.resolve(strict=True)
        if (
            resolved != candidate
            or not resolved.is_file()
            or not os.access(resolved, os.X_OK)
            or executable_sha256(resolved) != config.codex_cli_sha256
        ):
            raise UsageUnavailable(
                "Codex CLI changed since setup; review it and run `agw setup` again"
            )
    except OSError as exc:
        raise UsageUnavailable("Codex CLI is unavailable; install it and run `agw setup`") from exc
    return str(resolved)


def build_usage_report(paths: Paths) -> dict[str, object]:
    """Build the stable, secret-free unified usage document."""
    return {
        "generated_at": time.time(),
        "claude": read_claude_usage(paths),
        "codex": read_codex_usage(paths),
    }


def render_usage(report: dict[str, object]) -> str:
    """Render a compact terminal dashboard from :func:`build_usage_report`."""
    lines = ["Agent Gateway Usage", ""]
    claude = report.get("claude")
    lines.extend(_render_source("Claude Code", claude))
    lines.append("")
    codex = report.get("codex")
    lines.extend(_render_codex(codex))
    return "\n".join(lines)


def _render_source(title: str, source: object) -> list[str]:
    lines = [title]
    if not isinstance(source, dict) or source.get("status") != "ok":
        message = source.get("message") if isinstance(source, dict) else "unavailable"
        return [*lines, f"  unavailable — {message}"]
    windows = source.get("windows")
    if not isinstance(windows, list):
        return [*lines, "  unavailable — no usage windows returned"]
    for window in windows:
        if isinstance(window, dict):
            lines.append(_render_window(str(window.get("label") or "window"), window))
    captured = _timestamp(source.get("captured_at"))
    if captured is not None:
        lines.append(f"  updated {_age_label(captured)}")
    return lines


def _render_codex(source: object) -> list[str]:
    lines = ["Codex"]
    if not isinstance(source, dict) or source.get("status") != "ok":
        message = source.get("message") if isinstance(source, dict) else "unavailable"
        return [*lines, f"  unavailable — {message}"]
    plan = source.get("plan_type")
    if isinstance(plan, str):
        lines[0] += f" · {plan.title()}"
    buckets = source.get("buckets")
    if isinstance(buckets, list):
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            bucket_label = str(bucket.get("label") or "Codex")
            windows = bucket.get("windows")
            if not isinstance(windows, list):
                continue
            multiple_buckets = len(buckets) > 1
            for window in windows:
                if not isinstance(window, dict):
                    continue
                duration = str(window.get("label") or window.get("kind") or "window")
                label = f"{bucket_label} · {duration}" if multiple_buckets else duration
                lines.append(_render_window(label, window))
            reached = bucket.get("reached_type")
            if isinstance(reached, str):
                lines.append(f"  limit reached: {reached}")
    reset_count = source.get("reset_credits_available")
    if isinstance(reset_count, int):
        lines.append(f"  reset credits: {reset_count} available")
    credit_data = source.get("credits")
    if isinstance(credit_data, dict):
        if credit_data.get("unlimited") is True:
            lines.append("  workspace credits: unlimited")
        elif credit_data.get("hasCredits") is True:
            lines.append(f"  workspace credits: {credit_data.get('balance', 'available')}")
    summary = source.get("activity_summary")
    if isinstance(summary, dict):
        activity: list[str] = []
        lifetime = summary.get("lifetimeTokens")
        peak = summary.get("peakDailyTokens")
        streak = summary.get("currentStreakDays")
        if isinstance(lifetime, int):
            activity.append(f"{lifetime:,} lifetime tokens")
        if isinstance(peak, int):
            activity.append(f"{peak:,} peak daily")
        if isinstance(streak, int):
            activity.append(f"{streak} day streak")
        if activity:
            lines.append("  activity: " + " · ".join(activity))
    return lines


def _render_window(label: str, window: dict[str, object]) -> str:
    used = _number(window.get("used_percent")) or 0.0
    reset = _timestamp(window.get("resets_at"))
    suffix = f" · resets {_reset_label(reset)}" if reset is not None else ""
    return f"  {label:<18} {_bar(used)} {used:>5.1f}% used{suffix}"


def _bar(percent: float, width: int = 10) -> str:
    clamped = max(0.0, min(100.0, percent))
    filled = round(clamped / 100.0 * width)
    return "█" * filled + "░" * (width - filled)


def _duration_label(minutes: int) -> str:
    if minutes % (7 * 24 * 60) == 0:
        return f"{minutes // (7 * 24 * 60)}w"
    if minutes % (24 * 60) == 0:
        return f"{minutes // (24 * 60)}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def _reset_label(timestamp: float) -> str:
    try:
        value = datetime.fromtimestamp(timestamp).astimezone()
    except (OSError, OverflowError, ValueError):
        return "unknown"
    now = datetime.now().astimezone()
    if value.date() == now.date():
        return value.strftime("%H:%M")
    return value.strftime("%a %d %b %H:%M")


def _age_label(timestamp: float) -> str:
    seconds = max(0, int(time.time() - timestamp))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"
