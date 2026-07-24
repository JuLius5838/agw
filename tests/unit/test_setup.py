"""Unit tests for `agw setup` idempotency, non-destructiveness, and resolution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_gateway.config import executable_sha256, load_config
from agent_gateway.errors import ConfigError, PrerequisiteError
from agent_gateway.paths import Paths, get_paths
from agent_gateway.setup import run_setup
from agent_gateway.uninstall import run_uninstall


def _make_claude(tmp_path: Path) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    exe = bindir / "claude"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    return exe


def _env(tmp_path: Path) -> dict[str, str]:
    _make_claude(tmp_path)
    return {"HOME": str(tmp_path), "PATH": str(tmp_path / "bin")}


def _paths(tmp_path: Path) -> Paths:
    return get_paths({"HOME": str(tmp_path)})


def test_setup_installs_default_registry_and_config(tmp_path):
    result = run_setup(_paths(tmp_path), env=_env(tmp_path))
    assert result.default_model == "native Claude selection"
    assert result.active_models == []
    cfg = load_config(_paths(tmp_path))
    assert cfg.native_claude_path is not None


def test_setup_pins_codex_cli_identity_for_usage_queries(tmp_path) -> None:
    env = _env(tmp_path)
    codex = tmp_path / "bin" / "codex"
    codex.write_text("#!/bin/sh\nexit 0\n")
    codex.chmod(0o755)

    result = run_setup(_paths(tmp_path), env=env)

    cfg = load_config(_paths(tmp_path))
    assert cfg.codex_cli_path == str(codex.resolve())
    assert cfg.codex_cli_sha256 == executable_sha256(codex)
    assert any("pinned the current Codex CLI" in note for note in result.notes)


def test_setup_fails_without_native_claude(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(PrerequisiteError):
        run_setup(_paths(tmp_path), env={"HOME": str(tmp_path), "PATH": str(empty)})


def test_setup_is_idempotent_and_preserves_key(tmp_path):
    paths = _paths(tmp_path)
    run_setup(paths, env=_env(tmp_path))
    key1 = paths.proxy_key_file.read_text()
    models1 = paths.models_file.read_text()

    run_setup(paths, env=_env(tmp_path))
    assert paths.proxy_key_file.read_text() == key1  # key not rotated
    assert paths.models_file.read_text() == models1  # registry untouched


def test_setup_preserves_user_edited_registry(tmp_path):
    paths = _paths(tmp_path)
    run_setup(paths, env=_env(tmp_path))
    edited = paths.models_file.read_text() + "\n# my local note\n"
    paths.models_file.write_text(edited)
    run_setup(paths, env=_env(tmp_path))  # no default/owner changes
    assert paths.models_file.read_text() == edited  # preserved verbatim


def test_setup_agent_teams_opt_in_survives_without_shell(tmp_path):
    paths = _paths(tmp_path)
    run_setup(paths, agent_teams=True, no_shell=True, env=_env(tmp_path))
    cfg = load_config(paths)
    assert cfg.agent_teams_enabled is True
    assert cfg.shell_integration is None


def test_setup_provider_owner_resolves_duplicate(tmp_path):
    # A registry where the same public name has two candidates; owner picks one.
    paths = _paths(tmp_path)
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.models_file.write_text(
        """
default_model: shared
models:
  - name: shared
    provider: chatgpt
    upstream_model: chatgpt/shared
    mode: responses
    enabled: false
  - name: shared
    provider: copilot
    upstream_model: github_copilot/shared
    mode: chat
    enabled: false
"""
    )
    result = run_setup(
        paths, provider_owner=["shared=copilot"], default_model="shared", env=_env(tmp_path)
    )
    assert result.active_models == ["shared"]
    # The active one must be the copilot candidate.
    from agent_gateway.model_registry import load_registry

    assert load_registry(paths).get_active("shared").provider.value == "copilot"


def test_setup_rejects_unknown_provider_owner(tmp_path):
    with pytest.raises(ConfigError):
        run_setup(paths=_paths(tmp_path), provider_owner=["bad=notaprovider"], env=_env(tmp_path))


def test_uninstall_uses_persisted_custom_claude_config_dir(tmp_path) -> None:
    paths = _paths(tmp_path)
    custom_claude = tmp_path / "custom-claude"
    setup_env = {
        **_env(tmp_path),
        "CLAUDE_CONFIG_DIR": str(custom_claude),
    }
    run_setup(paths, no_shell=True, env=setup_env)
    skill = custom_claude / "skills" / "agw-usage" / "SKILL.md"
    assert skill.is_file()
    assert load_config(paths).claude_config_dir == str(custom_claude)

    run_uninstall(
        paths,
        env={
            "HOME": str(tmp_path / "different-home"),
            "PATH": setup_env["PATH"],
        },
    )

    assert not skill.exists()
    settings = json.loads((custom_claude / "settings.json").read_text())
    assert "statusLine" not in settings


def test_setup_migrates_managed_usage_between_claude_config_dirs(tmp_path) -> None:
    paths = _paths(tmp_path)
    first = tmp_path / "claude-a"
    second = tmp_path / "claude-b"
    base_env = _env(tmp_path)

    run_setup(
        paths,
        no_shell=True,
        env={**base_env, "CLAUDE_CONFIG_DIR": str(first)},
    )
    first_skill = first / "skills" / "agw-usage" / "SKILL.md"
    assert first_skill.is_file()

    run_setup(
        paths,
        no_shell=True,
        env={**base_env, "CLAUDE_CONFIG_DIR": str(second)},
    )
    second_skill = second / "skills" / "agw-usage" / "SKILL.md"
    assert not first_skill.exists()
    assert "statusLine" not in json.loads((first / "settings.json").read_text())
    assert second_skill.is_file()
    assert load_config(paths).claude_config_dir == str(second)

    run_uninstall(paths, env={"HOME": str(tmp_path / "other"), "PATH": base_env["PATH"]})

    assert not second_skill.exists()
    assert "statusLine" not in json.loads((second / "settings.json").read_text())


def test_setup_keeps_usage_when_config_changes_to_same_directory_alias(tmp_path) -> None:
    paths = _paths(tmp_path)
    real = tmp_path / "claude-real"
    real.mkdir()
    alias = tmp_path / "claude-alias"
    alias.symlink_to(real, target_is_directory=True)
    base_env = _env(tmp_path)

    run_setup(
        paths,
        no_shell=True,
        env={**base_env, "CLAUDE_CONFIG_DIR": str(alias)},
    )
    run_setup(
        paths,
        no_shell=True,
        env={**base_env, "CLAUDE_CONFIG_DIR": str(real)},
    )

    skill = real / "skills" / "agw-usage" / "SKILL.md"
    assert skill.is_file()
    settings = json.loads((real / "settings.json").read_text())
    assert settings["statusLine"]["command"].startswith("/")
    assert settings["statusLine"]["command"].endswith(
        " -I -m agent_gateway capture-claude-usage"
    )
    assert load_config(paths).claude_config_dir == str(real)
