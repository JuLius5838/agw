"""Unit tests for `agw setup` idempotency, non-destructiveness, and resolution."""

from __future__ import annotations

import json
import os
import stat
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


def test_setup_provider_owner_enables_exact_model(tmp_path):
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
"""
    )
    result = run_setup(
        paths, provider_owner=["shared=chatgpt"], default_model="shared", env=_env(tmp_path)
    )
    assert result.active_models == ["shared"]
    from agent_gateway.model_registry import load_registry

    assert load_registry(paths).get_active("shared").provider.value == "chatgpt"


def test_setup_removes_retired_provider_entries(tmp_path):
    paths = _paths(tmp_path)
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    original = """
# keep this rollback comment
default_model: legacy-model
models:
  - name: gpt-5.6-sol
    provider: chatgpt
    upstream_model: chatgpt/gpt-5.6-sol
    mode: responses
    enabled: true
  - name: legacy-model
    provider: copilot
    upstream_model: github_copilot/legacy-model
    mode: chat
    enabled: true
"""
    paths.models_file.write_text(original)

    result = run_setup(paths, no_shell=True, env=_env(tmp_path))

    from agent_gateway.model_registry import load_registry

    registry = load_registry(paths)
    assert registry.default_model is None
    assert [entry.name for entry in registry.active_models()] == ["gpt-5.6-sol"]
    assert any("copilot" in note for note in result.notes)
    assert "github_copilot/" not in paths.models_file.read_text()
    backup = paths.models_file.with_name("models.pre-retired-providers.yaml")
    assert backup.read_text() == original
    if os.name == "posix":
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert any(str(backup) in note for note in result.notes)

    backup.write_text("do not overwrite")
    run_setup(paths, no_shell=True, env=_env(tmp_path))
    assert backup.read_text() == "do not overwrite"


def test_setup_removes_inactive_retired_provider_candidate(tmp_path):
    paths = _paths(tmp_path)
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.models_file.write_text(
        """
default_model: null
models:
  - name: gpt-4.1
    provider: copilot
    upstream_model: github_copilot/gpt-4.1
    mode: chat
    enabled: false
"""
    )

    run_setup(paths, no_shell=True, env=_env(tmp_path))

    assert "copilot" not in paths.models_file.read_text()


def test_setup_clears_retired_default_when_supported_candidate_is_inactive(tmp_path):
    paths = _paths(tmp_path)
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.models_file.write_text(
        """
default_model: shared-model
models:
  - name: shared-model
    provider: chatgpt
    upstream_model: chatgpt/shared-model
    mode: responses
    enabled: false
  - name: shared-model
    provider: copilot
    upstream_model: github_copilot/shared-model
    mode: chat
    enabled: true
"""
    )

    run_setup(paths, no_shell=True, env=_env(tmp_path))

    from agent_gateway.model_registry import load_registry

    registry = load_registry(paths)
    assert registry.default_model is None
    assert registry.active_models() == ()


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
    assert settings["statusLine"]["command"].endswith(" -I -m agent_gateway capture-claude-usage")
    assert load_config(paths).claude_config_dir == str(real)
