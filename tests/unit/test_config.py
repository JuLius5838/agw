"""Unit tests for user configuration and native-Claude validation."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from agent_gateway.config import (
    DEFAULT_PROXY_PORT,
    GatewayConfig,
    discover_codex_cli,
    discover_native_claude,
    executable_sha256,
    load_config,
    save_config,
    validate_native_claude_path,
)
from agent_gateway.errors import ConfigError
from agent_gateway.paths import get_paths

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX permission semantics")


def test_missing_config_raises_config_error(tmp_path):
    p = get_paths({"HOME": str(tmp_path)})
    with pytest.raises(ConfigError):
        load_config(p)


def test_defaults():
    cfg = GatewayConfig()
    assert cfg.port == DEFAULT_PROXY_PORT
    assert cfg.native_claude_path is None
    assert cfg.agent_teams_enabled is False
    assert cfg.shell_integration is None
    assert cfg.default_effort is None
    assert cfg.claude_config_dir is None
    assert cfg.codex_cli_path is None
    assert cfg.codex_cli_sha256 is None


def test_roundtrip(tmp_path):
    p = get_paths({"HOME": str(tmp_path)})
    cfg = GatewayConfig(port=4100, native_claude_path="/usr/local/bin/claude")
    save_config(p, cfg)
    assert load_config(p) == cfg


def test_agent_teams_persist_independent_of_shell(tmp_path):
    # Declining shell integration must not clear the agent-team opt-in.
    p = get_paths({"HOME": str(tmp_path)})
    save_config(p, GatewayConfig(agent_teams_enabled=True, shell_integration=None))
    loaded = load_config(p)
    assert loaded.agent_teams_enabled is True
    assert loaded.shell_integration is None


def test_unknown_keys_rejected(tmp_path):
    p = get_paths({"HOME": str(tmp_path)})
    p.config_dir.mkdir(parents=True)
    p.config_file.write_text("port: 4000\nbogus_key: 1\n")
    with pytest.raises(ConfigError):
        load_config(p)


def test_invalid_port_rejected(tmp_path):
    p = get_paths({"HOME": str(tmp_path)})
    p.config_dir.mkdir(parents=True)
    p.config_file.write_text("port: 70000\n")
    with pytest.raises(ConfigError):
        load_config(p)


def test_invalid_default_effort_rejected(tmp_path):
    p = get_paths({"HOME": str(tmp_path)})
    p.config_dir.mkdir(parents=True)
    p.config_file.write_text("default_effort: ultracode\n")
    with pytest.raises(ConfigError):
        load_config(p)


@posix_only
def test_config_file_is_0600(tmp_path):
    p = get_paths({"HOME": str(tmp_path)})
    save_config(p, GatewayConfig())
    assert stat.S_IMODE(p.config_file.stat().st_mode) == 0o600


def _make_executable(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.write_text(body)
    path.chmod(0o755)
    return path


def test_validate_native_claude_accepts_executable(tmp_path):
    exe = _make_executable(tmp_path / "claude")
    assert validate_native_claude_path(exe) == Path(os.path.abspath(exe))


def test_discover_codex_cli_resolves_and_fingerprints_executable(tmp_path) -> None:
    executable = _make_executable(tmp_path / "codex")

    discovered = discover_codex_cli({"PATH": str(tmp_path)})

    assert discovered == (executable.resolve(), executable_sha256(executable))


def test_validate_native_claude_preserves_symlink_for_update_stability(tmp_path):
    # ~/.local/bin/claude is a symlink Claude repoints on update; we must store
    # the symlink path, not the version-pinned target.
    versioned = _make_executable(tmp_path / "claude-2.1.217")
    link = tmp_path / "claude"
    link.symlink_to(versioned)
    result = validate_native_claude_path(link)
    assert result == Path(os.path.abspath(link))
    assert result != versioned  # not resolved to the version-pinned binary


def test_validate_native_claude_rejects_missing(tmp_path):
    with pytest.raises(ConfigError):
        validate_native_claude_path(tmp_path / "does-not-exist")


@posix_only
def test_validate_native_claude_rejects_non_executable(tmp_path):
    f = tmp_path / "claude"
    f.write_text("not exec")
    f.chmod(0o644)
    with pytest.raises(ConfigError):
        validate_native_claude_path(f)


def test_validate_native_claude_rejects_agw_recursion(tmp_path):
    exe = _make_executable(tmp_path / "agw")
    with pytest.raises(ConfigError, match="recurse"):
        validate_native_claude_path(exe)


def test_discover_native_claude_finds_on_path(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _make_executable(bindir / "claude")
    found = discover_native_claude({"PATH": str(bindir), "HOME": str(tmp_path)})
    assert found == (bindir / "claude").resolve()


def test_discover_native_claude_absent(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert discover_native_claude({"PATH": str(empty), "HOME": str(tmp_path)}) is None
