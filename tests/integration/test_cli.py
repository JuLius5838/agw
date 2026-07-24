"""Integration tests for the ``agw`` command surface.

These exercise the CLI as real processes so we validate the frozen command tree,
argument passthrough, and the module/console entry-point parity — not internals.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from agent_gateway.errors import ExitCode

MVP_COMMANDS = [
    "setup",
    "auth",
    "models",
    "usage",
    "proxy",
    "claude",
    "shell",
    "doctor",
    "status",
    "logs",
    "uninstall",
]


def _run_module(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agent_gateway", *args],
        capture_output=True,
        text=True,
    )


def _run_console(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["agw", *args], capture_output=True, text=True)


def test_help_lists_mvp_commands() -> None:
    result = _run_module("--help")
    assert result.returncode == 0
    for command in MVP_COMMANDS:
        assert command in result.stdout, f"expected `{command}` in --help output"


def test_help_lists_no_unexpected_top_level_commands() -> None:
    # Guard against accidentally exposing extra top-level commands beyond the MVP set.
    result = _run_module("--help")
    assert result.returncode == 0
    # Role aliases and provider prefixes must never leak into the surface.
    assert "team-" not in result.stdout
    assert "chatgpt/" not in result.stdout


def test_unknown_command_is_nonzero() -> None:
    result = _run_module("definitely-not-a-command")
    assert result.returncode != 0


def test_auth_help_lists_supported_providers() -> None:
    result = _run_module("auth", "--help")
    assert result.returncode == 0
    assert "chatgpt" in result.stdout
    assert "copilot" in result.stdout


def test_version_reports_semver() -> None:
    result = _run_module("--version")
    assert result.returncode == 0
    assert result.stdout.strip().startswith("agw ")


def test_module_and_console_entrypoints_match() -> None:
    module = _run_module("--version")
    console = _run_console("--version")
    assert module.returncode == 0
    assert console.returncode == 0
    assert module.stdout.strip() == console.stdout.strip()


def test_runtime_command_without_setup_returns_config_exit_code(tmp_path) -> None:
    # A runtime command in an un-set-up HOME must fail with the stable CONFIG
    # exit code and a clear message — never an uncaught crash.
    env = {**os.environ, "HOME": str(tmp_path)}
    env.pop("XDG_CONFIG_HOME", None)
    env.pop("XDG_STATE_HOME", None)
    result = subprocess.run(
        [sys.executable, "-m", "agent_gateway", "status"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == ExitCode.CONFIG
    assert "set up" in result.stderr.lower()


def _isolated_env(home: Path, *, native_claude: bool = False) -> dict[str, str]:
    env = {**os.environ, "HOME": str(home)}
    env.pop("XDG_CONFIG_HOME", None)
    env.pop("XDG_STATE_HOME", None)
    if native_claude:
        bin_dir = home / "bin"
        bin_dir.mkdir(exist_ok=True)
        executable = bin_dir / "claude"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    return env


def _run_module_unconfigured(*args: str, home: str) -> subprocess.CompletedProcess[str]:
    """Run with an isolated, un-set-up HOME so `agw claude` hits a config error."""
    env = _isolated_env(Path(home))
    return subprocess.run(
        [sys.executable, "-m", "agent_gateway", *args],
        capture_output=True,
        text=True,
        env=env,
    )


def test_claude_passthrough_accepts_arbitrary_flags(tmp_path) -> None:
    # The launcher must not reinterpret Claude flags: unknown options, repeated
    # options, `--`, and positionals must be accepted (reaching the command body,
    # which fails with a CONFIG error because HOME is not set up) rather than
    # rejected as a Typer/Click usage error (exit 2).
    result = _run_module_unconfigured(
        "claude",
        "--version",
        "--model",
        "gpt-5.3-codex",
        "-p",
        "-p",
        "--",
        "extra-arg",
        home=str(tmp_path),
    )
    assert result.returncode != 2  # not a usage error — flags were accepted
    assert result.returncode == ExitCode.CONFIG  # reached the body; not set up


def test_claude_help_is_not_intercepted_by_agw(tmp_path) -> None:
    # `agw claude --help` must forward `--help` to Claude, so agw itself must not
    # treat it as its own help request (which would exit 0).
    result = _run_module_unconfigured("claude", "--help", home=str(tmp_path))
    assert result.returncode != 0


def test_models_list_json_after_setup(tmp_path) -> None:
    env = _isolated_env(tmp_path, native_claude=True)

    setup = subprocess.run(
        [sys.executable, "-m", "agent_gateway", "setup"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert setup.returncode == 0, setup.stderr

    listed = subprocess.run(
        [sys.executable, "-m", "agent_gateway", "models", "list", "--json"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert listed.returncode == 0, listed.stderr
    doc = json.loads(listed.stdout)
    assert doc["default_model"] is None
    assert not doc["models"]


def test_models_add_and_remove_roundtrip(tmp_path) -> None:
    env = _isolated_env(tmp_path, native_claude=True)

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "agent_gateway", *args],
            capture_output=True,
            text=True,
            env=env,
        )

    assert run("setup").returncode == 0

    added = run("models", "add", "gpt-5.6-example")
    assert added.returncode == 0, added.stderr
    doc = json.loads(run("models", "list", "--json").stdout)
    assert "gpt-5.6-example" in {m["name"] for m in doc["models"]}

    # Re-adding the same name+provider is rejected rather than silently overwriting.
    assert run("models", "add", "gpt-5.6-example").returncode != 0

    removed = run("models", "remove", "gpt-5.6-example")
    assert removed.returncode == 0, removed.stderr
    doc_after = json.loads(run("models", "list", "--json").stdout)
    assert "gpt-5.6-example" not in {m["name"] for m in doc_after["models"]}

    # Removing an unknown model is a clean model-unavailable failure, not a crash.
    assert run("models", "remove", "gpt-5.6-example").returncode == ExitCode.MODEL_UNAVAILABLE


def test_models_add_and_remove_copilot_roundtrip(tmp_path) -> None:
    env = _isolated_env(tmp_path, native_claude=True)

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "agent_gateway", *args],
            capture_output=True,
            text=True,
            env=env,
        )

    assert run("setup").returncode == 0
    added = run(
        "models",
        "add",
        "gpt-5.3-codex",
        "--provider",
        "copilot",
        "--mode",
        "responses",
    )
    assert added.returncode == 0, added.stderr

    doc = json.loads(run("models", "list", "--all", "--json").stdout)
    candidate = next(model for model in doc["models"] if model["name"] == "gpt-5.3-codex")
    assert candidate["provider"] == "copilot"
    assert candidate["mode"] == "responses"
    models_file = tmp_path / ".config" / "agent-gateway" / "models.yaml"
    assert "upstream_model: github_copilot/gpt-5.3-codex" in models_file.read_text()

    removed = run("models", "remove", "gpt-5.3-codex", "--provider", "copilot")
    assert removed.returncode == 0, removed.stderr


def test_usage_json_degrades_cleanly_without_provider_auth(tmp_path) -> None:
    env = {**os.environ, "HOME": str(tmp_path)}
    env.pop("XDG_CONFIG_HOME", None)
    env.pop("XDG_STATE_HOME", None)

    result = subprocess.run(
        [sys.executable, "-m", "agent_gateway", "usage", "--json"],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["claude"]["status"] == "unavailable"
    assert document["codex"]["status"] == "unavailable"


def _run_env(*args: str, home: str) -> subprocess.CompletedProcess[str]:
    env = _isolated_env(Path(home), native_claude=True)
    return subprocess.run(
        [sys.executable, "-m", "agent_gateway", *args], capture_output=True, text=True, env=env
    )


def test_setup_doctor_uninstall_roundtrip(tmp_path) -> None:
    home = str(tmp_path)
    assert _run_env("setup", home=home).returncode == 0
    usage_skill = tmp_path / ".claude" / "skills" / "agw-usage" / "SKILL.md"
    claude_settings = tmp_path / ".claude" / "settings.json"
    assert usage_skill.is_file()
    status_command = json.loads(claude_settings.read_text())["statusLine"]["command"]
    assert status_command.startswith("/")
    assert status_command.endswith(" -I -m agent_gateway capture-claude-usage")

    # Native Claude-only setup has no external provider that can fail offline.
    doctor = _run_env("doctor", home=home)
    assert "native claude" in doctor.stdout
    assert doctor.returncode == 0

    assert _run_env("status", home=home).returncode == 0

    # Default uninstall preserves credentials and removes generated runtime files.
    creds = tmp_path / ".config" / "agent-gateway" / "credentials"
    assert creds.exists()
    uninstall = _run_env("uninstall", home=home)
    assert uninstall.returncode == 0
    assert creds.exists()  # credentials preserved without --credentials
    assert not (tmp_path / ".config" / "agent-gateway" / "config.yaml").exists()
    assert not usage_skill.exists()
    assert "statusLine" not in json.loads(claude_settings.read_text())


def test_uninstall_credentials_non_tty_requires_yes(tmp_path) -> None:
    home = str(tmp_path)
    assert _run_env("setup", home=home).returncode == 0
    creds = tmp_path / ".config" / "agent-gateway" / "credentials"

    # Non-interactive --credentials without --yes must refuse and keep credentials.
    refused = _run_env("uninstall", "--credentials", home=home)
    assert refused.returncode == ExitCode.CONFIG
    assert creds.exists()

    # With --yes it proceeds.
    ok = _run_env("uninstall", "--credentials", "--yes", home=home)
    assert ok.returncode == 0
    assert not creds.exists()
