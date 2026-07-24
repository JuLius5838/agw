"""Contract test: `agw claude` execs the native binary with the right env & args.

Uses a fake 'claude' executable that records its argv and a slice of its
environment, then exits with a distinctive code. Seeds a fake ChatGPT credential
so the real proxy boots offline. Proves argument passthrough, the FR-21
environment, and exit-code forwarding through the real os.execve path.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from agent_gateway import proxy
from agent_gateway.config import GatewayConfig, save_config
from agent_gateway.paths import ensure_dir, get_paths
from agent_gateway.process import find_free_port

pytestmark = pytest.mark.contract

REGISTRY_TEXT = """
default_model: gpt-5.3-codex
models:
  - name: gpt-5.3-codex
    provider: chatgpt
    upstream_model: chatgpt/gpt-5.3-codex
    mode: responses
    enabled: true
"""

_CAPTURED_KEYS = [
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
    "ANTHROPIC_CUSTOM_MODEL_OPTION",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CHATGPT_TOKEN_DIR",
    "UNRELATED_VAR",
]

_FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
keys = {keys!r}
with open(os.environ["AGW_TEST_CAPTURE"], "w") as fh:
    json.dump({{"argv": sys.argv[1:], "env": {{k: os.environ.get(k) for k in keys}}}}, fh)
sys.exit(7)
"""


@pytest.fixture(scope="module")
def home(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    root = tmp_path_factory.mktemp("agw-launcher")
    paths = get_paths({"HOME": str(root)})

    fake_claude = root / "fake-claude"
    fake_claude.write_text(_FAKE_CLAUDE.format(keys=_CAPTURED_KEYS))
    fake_claude.chmod(0o755)

    token_dir = paths.provider_credentials_dir("chatgpt")
    ensure_dir(token_dir)
    (token_dir / "auth.json").write_text(
        json.dumps({"access_token": "fake", "refresh_token": "r", "expires_at": 9999999999})
    )

    save_config(paths, GatewayConfig(port=find_free_port(), native_claude_path=str(fake_claude)))
    paths.models_file.write_text(REGISTRY_TEXT)

    try:
        yield root
    finally:
        proxy.stop(paths)


def _run_claude(
    home: Path, capture: Path, extra_args: list[str]
) -> subprocess.CompletedProcess[str]:
    agw = Path(sys.executable).parent / "agw"
    env = {
        **os.environ,
        "HOME": str(home),
        "AGW_TEST_CAPTURE": str(capture),
        "CLAUDE_CODE_SUBAGENT_MODEL": "should-be-removed",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "UNRELATED_VAR": "keep-me",
    }
    env.pop("XDG_CONFIG_HOME", None)
    env.pop("XDG_STATE_HOME", None)
    return subprocess.run(
        [str(agw), "claude", *extra_args], env=env, capture_output=True, text=True, timeout=90
    )


def test_launch_forwards_args_env_and_exit_code(home: Path, tmp_path: Path) -> None:
    capture = tmp_path / "capture-explicit.json"
    result = _run_claude(home, capture, ["--model", "gpt-5.3-codex", "--", "extra-arg"])

    assert result.returncode == 7  # fake Claude's exit code passed through
    data = json.loads(capture.read_text())

    # Arguments forwarded verbatim, including `--` and the positional after it.
    assert data["argv"] == ["--model", "gpt-5.3-codex", "--", "extra-arg"]

    env = data["env"]
    assert env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
    assert env.get("ANTHROPIC_AUTH_TOKEN") is None
    assert env.get("ANTHROPIC_API_KEY") is None
    assert env["ANTHROPIC_CUSTOM_HEADERS"].startswith("X-AGW-Key: sk-agw-")
    assert env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] == "1"
    assert env["ANTHROPIC_CUSTOM_MODEL_OPTION"] == "gpt-5.3-codex"
    assert env["ANTHROPIC_CUSTOM_MODEL_OPTION_NAME"] == "gpt-5.3-codex"
    assert "Agent Gateway" in env["ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION"]
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] is None
    assert env["ANTHROPIC_MODEL"] is None  # suppressed by explicit --model
    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] is None  # stripped
    assert env["CLAUDE_CODE_USE_BEDROCK"] is None  # stripped
    assert env["CHATGPT_TOKEN_DIR"] is None  # never leaked to Claude
    assert env["UNRELATED_VAR"] == "keep-me"  # preserved


def test_launch_without_model_uses_default(home: Path, tmp_path: Path) -> None:
    capture = tmp_path / "capture-default.json"
    result = _run_claude(home, capture, ["chat"])
    assert result.returncode == 7
    data = json.loads(capture.read_text())
    assert data["argv"] == ["chat"]
    assert data["env"]["ANTHROPIC_MODEL"] == "gpt-5.3-codex"
