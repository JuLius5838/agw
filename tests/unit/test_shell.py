"""Unit tests for the reversible bare-`claude` shell integration."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_gateway import shell
from agent_gateway.config import GatewayConfig, load_config
from agent_gateway.errors import ConfigError
from agent_gateway.paths import Paths, get_paths
from agent_gateway.shell import END_MARKER, START_MARKER


def _paths(home: Path) -> Paths:
    return get_paths({"HOME": str(home)})


def _env(home: Path, shell_name: str = "zsh") -> dict[str, str]:
    return {"HOME": str(home), "SHELL": f"/bin/{shell_name}"}


def test_enable_adds_block_and_source_file_and_persists_choice(tmp_path):
    paths = _paths(tmp_path)
    result = shell.enable(paths, GatewayConfig(), "zsh", _env(tmp_path))
    assert result.changed is True

    startup = tmp_path / ".zshrc"
    content = startup.read_text()
    assert START_MARKER in content and END_MARKER in content
    assert paths.shell_source_file("zsh").is_file()
    assert load_config(paths).shell_integration == "zsh"


def test_enable_is_idempotent(tmp_path):
    paths = _paths(tmp_path)
    shell.enable(paths, GatewayConfig(), "zsh", _env(tmp_path))
    second = shell.enable(paths, load_config(paths), "zsh", _env(tmp_path))
    assert second.changed is False
    content = (tmp_path / ".zshrc").read_text()
    assert content.count(START_MARKER) == 1


def test_disable_removes_block_and_restores_content(tmp_path):
    paths = _paths(tmp_path)
    startup = tmp_path / ".zshrc"
    startup.write_text("export FOO=1\n")

    shell.enable(paths, GatewayConfig(), "zsh", _env(tmp_path))
    shell.disable(paths, load_config(paths), "zsh", _env(tmp_path))

    assert startup.read_text() == "export FOO=1\n"  # byte-for-byte restored
    assert not paths.shell_source_file("zsh").exists()
    assert load_config(paths).shell_integration is None


def test_enable_refuses_foreign_claude_definition(tmp_path):
    startup = tmp_path / ".zshrc"
    startup.write_text("claude() { echo custom; }\n")
    with pytest.raises(ConfigError, match="already defined"):
        shell.enable(_paths(tmp_path), GatewayConfig(), "zsh", _env(tmp_path))


def test_enable_refuses_malformed_markers(tmp_path):
    startup = tmp_path / ".zshrc"
    startup.write_text(f"{START_MARKER}\nsome half-block\n")  # start without end
    with pytest.raises(ConfigError, match="malformed"):
        shell.enable(_paths(tmp_path), GatewayConfig(), "zsh", _env(tmp_path))


def test_zdotdir_is_respected(tmp_path):
    zdotdir = tmp_path / "zdot"
    zdotdir.mkdir()
    env = {"HOME": str(tmp_path), "SHELL": "/bin/zsh", "ZDOTDIR": str(zdotdir)}
    shell.enable(_paths(tmp_path), GatewayConfig(), "zsh", env)
    assert (zdotdir / ".zshrc").is_file()
    assert not (tmp_path / ".zshrc").exists()


def test_status_reflects_enable_disable(tmp_path):
    env = _env(tmp_path)
    paths = _paths(tmp_path)
    assert shell.is_enabled("zsh", env) is False
    shell.enable(paths, GatewayConfig(), "zsh", env)
    assert shell.is_enabled("zsh", env) is True
    shell.disable(paths, load_config(paths), "zsh", env)
    assert shell.is_enabled("zsh", env) is False


def test_resolve_shell_from_env_and_explicit_and_unknown(tmp_path):
    assert shell.resolve_shell(None, {"SHELL": "/usr/bin/bash"}) == "bash"
    assert shell.resolve_shell("zsh", {}) == "zsh"
    with pytest.raises(ConfigError):
        shell.resolve_shell(None, {"SHELL": "/usr/bin/fish"})
