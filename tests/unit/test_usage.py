"""Offline tests for the unified Claude/Codex usage dashboard."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import agent_gateway.usage as usage
from agent_gateway.config import GatewayConfig, executable_sha256, save_config
from agent_gateway.paths import get_paths


def _paths(tmp_path: Path):
    return get_paths({"HOME": str(tmp_path)})


def test_capture_claude_status_persists_only_documented_limits(tmp_path) -> None:
    paths = _paths(tmp_path)
    payload = {
        "session_id": "secret-session",
        "access_token": "never-persist-this",
        "rate_limits": {
            "five_hour": {"used_percentage": 42.5, "resets_at": 1_800_000_000},
            "seven_day": {"used_percentage": 13, "resets_at": 1_800_086_400},
            "future_field": {"private": "ignored"},
        },
    }

    assert usage.capture_claude_status(paths, json.dumps(payload)) is True
    raw = paths.claude_usage_file.read_text()
    assert "secret-session" not in raw
    assert "never-persist-this" not in raw
    assert "future_field" not in raw
    snapshot = json.loads(raw)
    assert snapshot["rate_limits"]["five_hour"]["used_percentage"] == 42.5
    assert snapshot["rate_limits"]["seven_day"]["used_percentage"] == 13.0


def test_capture_without_limits_keeps_previous_snapshot(tmp_path) -> None:
    paths = _paths(tmp_path)
    first = json.dumps(
        {"rate_limits": {"five_hour": {"used_percentage": 10, "resets_at": 2_000_000_000}}}
    )
    assert usage.capture_claude_status(paths, first) is True
    before = paths.claude_usage_file.read_bytes()

    assert usage.capture_claude_status(paths, '{"rate_limits": null}') is False
    assert paths.claude_usage_file.read_bytes() == before


def test_malformed_jwt_claims_are_treated_as_missing() -> None:
    assert usage._jwt_claims("not-a-jwt") == {}
    assert usage._jwt_claims("x.%%%invalid%%%.y") == {}


def test_expired_chatgpt_credential_is_not_mutated(tmp_path) -> None:
    paths = _paths(tmp_path)
    auth_file = paths.provider_credentials_dir("chatgpt") / "auth.json"
    auth_file.parent.mkdir(parents=True)
    auth_file.write_text(
        json.dumps(
            {
                "access_token": "expired",
                "refresh_token": "refresh",
                "account_id": "account",
                "expires_at": 1,
            }
        )
    )
    before = auth_file.read_bytes()

    try:
        usage._load_chatgpt_credentials(paths)
    except usage.UsageUnavailable as exc:
        assert "needs refresh" in str(exc)
    else:
        raise AssertionError("expected an expired credential to be rejected")

    assert auth_file.read_bytes() == before


def test_read_claude_usage_normalizes_windows(tmp_path) -> None:
    paths = _paths(tmp_path)
    usage.capture_claude_status(
        paths,
        json.dumps(
            {
                "rate_limits": {
                    "five_hour": {"used_percentage": 20, "resets_at": 2_000_000_000},
                    "seven_day": {"used_percentage": 70, "resets_at": 2_000_100_000},
                }
            }
        ),
    )

    result = usage.read_claude_usage(paths)

    assert result["status"] == "ok"
    windows = result["windows"]
    assert isinstance(windows, list)
    assert [window["label"] for window in windows] == ["5 hour", "7 day"]


def test_normalize_codex_usage_keeps_all_buckets_and_activity() -> None:
    rate_result = {
        "rateLimitsByLimitId": {
            "codex": {
                "limitId": "codex",
                "limitName": None,
                "primary": {
                    "usedPercent": 38,
                    "windowDurationMins": 10_080,
                    "resetsAt": 2_000_000_000,
                },
                "secondary": {
                    "usedPercent": 12,
                    "windowDurationMins": 300,
                    "resetsAt": 2_000_000_100,
                },
                "planType": "plus",
                "credits": {
                    "hasCredits": False,
                    "unlimited": False,
                    "balance": "0",
                    "accessToken": "must-not-escape",
                },
            },
            "codex_other": {
                "limitId": "codex_other",
                "limitName": "Review",
                "primary": {
                    "usedPercent": 4,
                    "windowDurationMins": 60,
                    "resetsAt": 2_000_000_200,
                },
            },
        },
        "rateLimitResetCredits": {"availableCount": 2},
    }
    activity: dict[str, object] = {
        "summary": {
            "lifetimeTokens": 1_234_567,
            "peakDailyTokens": 45_678,
            "currentStreakDays": 8,
            "accessToken": "must-not-escape",
        },
        "dailyUsageBuckets": [
            {
                "startDate": "2026-07-23\u001b[31m",
                "tokens": 1234,
                "jwt": "must-not-escape",
            }
        ],
    }

    result = usage._normalize_codex_usage(rate_result, activity)

    assert result["status"] == "ok"
    assert result["plan_type"] == "plus"
    assert result["reset_credits_available"] == 2
    buckets = result["buckets"]
    assert isinstance(buckets, list)
    assert len(buckets) == 2
    assert buckets[0]["windows"][0]["label"] == "1w"
    assert buckets[0]["windows"][1]["label"] == "5h"
    assert result["activity_summary"] == {
        "lifetimeTokens": 1_234_567,
        "peakDailyTokens": 45_678,
        "currentStreakDays": 8,
    }
    serialized = json.dumps(result)
    assert "must-not-escape" not in serialized
    assert "\u001b" not in serialized


def test_codex_version_gate_accepts_only_tested_series(monkeypatch) -> None:
    def completed(version: str):
        return subprocess.CompletedProcess(
            args=["codex", "--version"],
            returncode=0,
            stdout=f"codex-cli {version}\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: completed("0.145.7"))
    usage._require_supported_codex("/fake/codex")

    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: completed("0.146.0"))
    try:
        usage._require_supported_codex("/fake/codex")
    except usage.UsageUnavailable as exc:
        assert "unverified" in str(exc)
        assert "0.145.x" in str(exc)
    else:
        raise AssertionError("expected an unverified Codex version to be rejected")


def test_app_server_reader_handles_batched_notification_and_response(tmp_path) -> None:
    executable = tmp_path / "fake-codex"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import sys

for line in sys.stdin:
    message = json.loads(line)
    request_id = message.get("id")
    if request_id is None:
        continue
    notification = {"method": "test/notification", "params": {"batched": True}}
    response = {"id": request_id, "result": {"method": message["method"]}}
    sys.stdout.write(json.dumps(notification) + "\\n" + json.dumps(response) + "\\n")
    sys.stdout.flush()
"""
    )
    executable.chmod(0o755)

    with usage._AppServer(str(executable), dict(os.environ), timeout=2) as server:
        first = server.request("initialize", {})
        second = server.request("account/rateLimits/read")

    assert first == {"method": "initialize"}
    assert second == {"method": "account/rateLimits/read"}


def test_read_codex_usage_uses_agw_token_in_isolated_app_server(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    token = "test-access-token"
    requests: list[tuple[str, dict[str, object] | None]] = []

    class FakeServer:
        def __init__(self, executable, env):
            assert executable == "/fake/codex"
            assert env["CODEX_HOME"] != str(tmp_path / ".codex")
            assert env["PATH"] == os.environ["PATH"]
            assert "ANTHROPIC_CUSTOM_HEADERS" not in env
            assert "ANTHROPIC_API_KEY" not in env
            assert "CHATGPT_TOKEN_DIR" not in env
            assert "AGW_TEST_SECRET" not in env

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def notify(self, _method, _params=None):
            return None

        def request(self, method, params=None):
            requests.append((method, params))
            if method == "account/rateLimits/read":
                return {
                    "rateLimits": {
                        "limitId": "codex",
                        "primary": {
                            "usedPercent": 10,
                            "windowDurationMins": 60,
                            "resetsAt": 2_000_000_000,
                        },
                    }
                }
            if method == "account/usage/read":
                return {"summary": {"lifetimeTokens": 99}}
            return {}

    monkeypatch.setattr(
        usage,
        "_trusted_codex_executable",
        lambda _paths: "/fake/codex",
    )
    monkeypatch.setattr(
        usage,
        "_require_supported_codex",
        lambda _executable, *, env=None: None,
    )
    monkeypatch.setattr(usage, "_load_chatgpt_credentials", lambda _paths: (token, "acct"))
    monkeypatch.setattr(usage, "_AppServer", FakeServer)
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "X-AGW-Key: secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", "/secret/provider")
    monkeypatch.setenv("AGW_TEST_SECRET", "secret")

    result = usage.read_codex_usage(paths)

    assert result["status"] == "ok"
    login = next(params for method, params in requests if method == "account/login/start")
    assert login == {
        "type": "chatgptAuthTokens",
        "accessToken": token,
        "chatgptAccountId": "acct",
    }
    assert token not in json.dumps(result)


def test_trusted_codex_rejects_executable_changed_since_setup(tmp_path) -> None:
    paths = _paths(tmp_path)
    executable = tmp_path / "codex"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    save_config(
        paths,
        GatewayConfig(
            codex_cli_path=str(executable),
            codex_cli_sha256=executable_sha256(executable),
        ),
    )
    executable.write_text("#!/bin/sh\nsend-token-somewhere\n")

    with pytest.raises(usage.UsageUnavailable, match="changed since setup"):
        usage._trusted_codex_executable(paths)


def test_render_usage_includes_limits_credits_and_activity() -> None:
    report: dict[str, object] = {
        "claude": {
            "status": "ok",
            "captured_at": 2_000_000_000,
            "windows": [
                {
                    "label": "5 hour",
                    "used_percent": 42,
                    "resets_at": 2_000_000_100,
                }
            ],
        },
        "codex": {
            "status": "ok",
            "plan_type": "plus",
            "buckets": [
                {
                    "label": "Codex",
                    "windows": [
                        {
                            "label": "1w",
                            "used_percent": 38,
                            "resets_at": 2_000_000_200,
                        }
                    ],
                }
            ],
            "reset_credits_available": 2,
            "activity_summary": {
                "lifetimeTokens": 1_234,
                "peakDailyTokens": 456,
                "currentStreakDays": 3,
            },
        },
    }

    rendered = usage.render_usage(report)

    assert "Claude Code" in rendered
    assert "5 hour" in rendered
    assert "Codex · Plus" in rendered
    assert "1w" in rendered
    assert "reset credits: 2 available" in rendered
    assert "1,234 lifetime tokens" in rendered
