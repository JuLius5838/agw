"""Tests for the generated standalone Claude ``/agw-usage`` integration."""

from __future__ import annotations

import hashlib
import json

from agent_gateway.claude_integration import (
    _PLUGIN_FILES,
    install_claude_usage,
    uninstall_claude_usage,
)


def test_install_adds_skill_and_preserves_other_settings(tmp_path) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.json"
    settings_path.write_text(json.dumps({"effortLevel": "xhigh"}))

    result = install_claude_usage({"HOME": str(tmp_path)})

    assert result.skill_installed is True
    assert result.collector_enabled is True
    assert result.skill_path == claude_dir / "skills" / "agw-usage" / "SKILL.md"
    assert "name: agw-usage" in result.skill_path.read_text()
    settings = json.loads(settings_path.read_text())
    assert settings["effortLevel"] == "xhigh"
    assert settings["statusLine"]["type"] == "command"
    assert settings["statusLine"]["padding"] == 0
    assert settings["statusLine"]["command"].startswith("/")
    assert settings["statusLine"]["command"].endswith(" -I -m agent_gateway capture-claude-usage")
    plugin_dir = claude_dir / "skills" / "agent-gateway"
    assert (plugin_dir / ".claude-plugin" / "plugin.json").is_file()
    assert (plugin_dir / ".mcp.json").is_file()
    hooks = json.loads((plugin_dir / "hooks" / "hooks.json").read_text())
    handler = hooks["hooks"]["UserPromptExpansion"][0]
    assert handler["matcher"] == "agw-usage"
    hook_types = {hook["type"] for hook in handler["hooks"]}
    assert hook_types == {"command", "mcp_tool"}
    command_hook = next(hook for hook in handler["hooks"] if hook["type"] == "command")
    assert command_hook["command"].startswith("/")
    assert command_hook["args"] == [
        "-I",
        "-m",
        "agent_gateway",
        "claude-usage-guard",
    ]
    assert "__AGW_" not in json.dumps(hooks)
    mcp = json.loads((plugin_dir / ".mcp.json").read_text())
    server = mcp["mcpServers"]["usage-ui"]
    assert server["command"] == command_hook["command"]
    assert server["args"] == [
        "-I",
        "-m",
        "agent_gateway",
        "claude-extension-server",
    ]


def test_install_is_idempotent(tmp_path) -> None:
    env = {"HOME": str(tmp_path)}
    first = install_claude_usage(env)
    first_settings = (tmp_path / ".claude" / "settings.json").read_bytes()

    second = install_claude_usage(env)

    assert second.collector_enabled is True
    assert second.skill_path == first.skill_path
    assert (tmp_path / ".claude" / "settings.json").read_bytes() == first_settings


def test_install_preserves_existing_custom_statusline(tmp_path) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.json"
    custom = {"type": "command", "command": "~/bin/my-status", "padding": 1}
    settings_path.write_text(json.dumps({"statusLine": custom, "effortLevel": "max"}))

    result = install_claude_usage({"HOME": str(tmp_path)})

    assert result.collector_enabled is False
    settings = json.loads(settings_path.read_text())
    assert settings["statusLine"] == custom
    assert settings["effortLevel"] == "max"
    assert result.skill_path.is_file()


def test_install_upgrades_legacy_statusline_to_pinned_runtime(tmp_path) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "statusLine": {
                    "type": "command",
                    "command": "agw capture-claude-usage",
                    "padding": 0,
                }
            }
        )
    )

    result = install_claude_usage({"HOME": str(tmp_path)})

    settings = json.loads(settings_path.read_text())
    assert settings["statusLine"]["command"].startswith("/")
    assert settings["statusLine"]["command"].endswith(" -I -m agent_gateway capture-claude-usage")
    assert result.collector_enabled is True
    assert any("upgraded Claude usage capture" in note for note in result.notes)


def test_uninstall_removes_statusline_owned_by_previous_runtime(tmp_path, monkeypatch) -> None:
    runtime_a = tmp_path / "runtime-a" / "python"
    runtime_a.parent.mkdir()
    runtime_a.write_text("#!/bin/sh\nexit 0\n")
    runtime_a.chmod(0o755)
    runtime_b = tmp_path / "runtime-b" / "python"
    runtime_b.parent.mkdir()
    runtime_b.write_text("#!/bin/sh\nexit 0\n")
    runtime_b.chmod(0o755)
    monkeypatch.setattr("sys.executable", str(runtime_a))
    install_claude_usage({"HOME": str(tmp_path)})
    settings_path = tmp_path / ".claude" / "settings.json"
    assert str(runtime_a) in json.loads(settings_path.read_text())["statusLine"]["command"]

    monkeypatch.setattr("sys.executable", str(runtime_b))
    uninstall_claude_usage({"HOME": str(tmp_path)})

    assert "statusLine" not in json.loads(settings_path.read_text())
    assert not (tmp_path / ".claude" / ".agw-statusline-owned.json").exists()


def test_install_upgrades_statusline_owned_by_previous_runtime(tmp_path, monkeypatch) -> None:
    runtime_a = tmp_path / "runtime-a" / "python"
    runtime_a.parent.mkdir()
    runtime_a.write_text("#!/bin/sh\nexit 0\n")
    runtime_a.chmod(0o755)
    runtime_b = tmp_path / "runtime-b" / "python"
    runtime_b.parent.mkdir()
    runtime_b.write_text("#!/bin/sh\nexit 0\n")
    runtime_b.chmod(0o755)
    monkeypatch.setattr("sys.executable", str(runtime_a))
    install_claude_usage({"HOME": str(tmp_path)})

    monkeypatch.setattr("sys.executable", str(runtime_b))
    result = install_claude_usage({"HOME": str(tmp_path)})

    settings_path = tmp_path / ".claude" / "settings.json"
    command = json.loads(settings_path.read_text())["statusLine"]["command"]
    assert str(runtime_b) in command
    assert str(runtime_a) not in command
    assert result.collector_enabled is True


def test_uninstall_preserves_matching_statusline_without_ownership_marker(
    tmp_path,
) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.json"
    statusline = {
        "type": "command",
        "command": "agw capture-claude-usage",
        "padding": 0,
    }
    settings_path.write_text(json.dumps({"statusLine": statusline}))

    uninstall_claude_usage({"HOME": str(tmp_path)})

    assert json.loads(settings_path.read_text())["statusLine"] == statusline


def test_uninstall_preserves_statusline_with_invalid_ownership_marker(
    tmp_path,
) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.json"
    statusline = {
        "type": "command",
        "command": "/managed/python -I -m agent_gateway capture-claude-usage",
        "padding": 0,
    }
    settings_path.write_text(json.dumps({"statusLine": statusline}))
    (claude_dir / ".agw-statusline-owned.json").write_text(
        json.dumps({"owner": "someone-else", "command": statusline["command"]})
    )

    uninstall_claude_usage({"HOME": str(tmp_path)})

    assert json.loads(settings_path.read_text())["statusLine"] == statusline
    assert (claude_dir / ".agw-statusline-owned.json").is_file()


def test_install_does_not_replace_invalid_settings(tmp_path) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.json"
    settings_path.write_text("{broken")

    result = install_claude_usage({"HOME": str(tmp_path)})

    assert result.collector_enabled is False
    assert settings_path.read_text() == "{broken"
    assert result.skill_path.is_file()


def test_install_preserves_unrelated_skill_collision(tmp_path) -> None:
    skill_path = tmp_path / ".claude" / "skills" / "agw-usage" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("---\nname: agw-usage\n---\nMy command.\n")

    result = install_claude_usage({"HOME": str(tmp_path)})

    assert result.skill_installed is False
    assert skill_path.read_text().endswith("My command.\n")
    assert not (skill_path.parent / ".agw-owned.json").exists()


def test_install_honors_absolute_claude_config_dir(tmp_path) -> None:
    custom = tmp_path / "custom-claude"

    result = install_claude_usage(
        {
            "HOME": str(tmp_path / "home"),
            "CLAUDE_CONFIG_DIR": str(custom),
        }
    )

    assert result.skill_installed is True
    assert result.skill_path == custom / "skills" / "agw-usage" / "SKILL.md"
    assert (custom / "settings.json").is_file()
    assert not (tmp_path / "home" / ".claude").exists()


def test_uninstall_removes_only_managed_artifacts(tmp_path) -> None:
    env = {"HOME": str(tmp_path)}
    result = install_claude_usage(env)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text())
    settings["effortLevel"] = "xhigh"
    settings_path.write_text(json.dumps(settings))

    notes = uninstall_claude_usage(env)

    assert not result.skill_path.exists()
    assert not (result.skill_path.parent / ".agw-owned.json").exists()
    assert not (tmp_path / ".claude" / "skills" / "agent-gateway").exists()
    remaining = json.loads(settings_path.read_text())
    assert remaining == {"effortLevel": "xhigh"}
    assert any("removed managed Claude command" in note for note in notes)


def test_uninstall_preserves_modified_skill_and_custom_statusline(tmp_path) -> None:
    env = {"HOME": str(tmp_path)}
    result = install_claude_usage(env)
    result.skill_path.write_text(result.skill_path.read_text() + "\nUser modification.\n")
    settings_path = tmp_path / ".claude" / "settings.json"
    custom = {"type": "command", "command": "~/bin/custom"}
    settings_path.write_text(json.dumps({"statusLine": custom}))

    notes = uninstall_claude_usage(env)

    assert result.skill_path.is_file()
    assert "User modification" in result.skill_path.read_text()
    assert json.loads(settings_path.read_text())["statusLine"] == custom
    assert any("kept modified" in note for note in notes)


def test_install_and_uninstall_preserve_symlink_managed_files(tmp_path) -> None:
    claude_dir = tmp_path / ".claude"
    skill_path = claude_dir / "skills" / "agw-usage" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_target = tmp_path / "dotfiles-skill.md"
    skill_target.write_text("---\nname: agw-usage\n---\nDotfiles command.\n")
    skill_path.symlink_to(skill_target)

    settings_target = tmp_path / "dotfiles-settings.json"
    settings_target.write_text(json.dumps({"effortLevel": "high"}))
    settings_path = claude_dir / "settings.json"
    settings_path.symlink_to(settings_target)

    result = install_claude_usage({"HOME": str(tmp_path)})
    uninstall_claude_usage({"HOME": str(tmp_path)})

    assert result.skill_installed is False
    assert skill_path.is_symlink()
    assert settings_path.is_symlink()
    assert "Dotfiles command" in skill_target.read_text()
    assert json.loads(settings_target.read_text()) == {"effortLevel": "high"}


def test_install_preserves_modified_companion_plugin_file(tmp_path) -> None:
    env = {"HOME": str(tmp_path)}
    install_claude_usage(env)
    plugin_dir = tmp_path / ".claude" / "skills" / "agent-gateway"
    hooks_path = plugin_dir / "hooks" / "hooks.json"
    hooks_path.write_text('{"user":"custom"}\n')

    result = install_claude_usage(env)
    notes = uninstall_claude_usage(env)

    assert hooks_path.read_text() == '{"user":"custom"}\n'
    assert hooks_path.is_file()
    assert any("incomplete" in note for note in result.notes)
    assert any("kept modified Claude plugin" in note for note in notes)


def test_uninstall_ignores_unrecognized_paths_in_forged_marker(tmp_path) -> None:
    env = {"HOME": str(tmp_path)}
    install_claude_usage(env)
    plugin_dir = tmp_path / ".claude" / "skills" / "agent-gateway"
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("keep me\n")
    marker_path = plugin_dir / ".agw-owned.json"
    marker = json.loads(marker_path.read_text())
    marker["files"]["../../../unrelated.txt"] = hashlib.sha256(unrelated.read_bytes()).hexdigest()
    marker_path.write_text(json.dumps(marker))

    uninstall_claude_usage(env)

    assert unrelated.read_text() == "keep me\n"


def test_install_rejects_symlinked_plugin_subdirectory(tmp_path) -> None:
    claude_dir = tmp_path / ".claude"
    plugin_dir = claude_dir / "skills" / "agent-gateway"
    plugin_dir.mkdir(parents=True)
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    (plugin_dir / "hooks").symlink_to(redirected, target_is_directory=True)

    result = install_claude_usage({"HOME": str(tmp_path)})

    assert not (redirected / "hooks.json").exists()
    assert any("incomplete" in note for note in result.notes)


def test_packaged_plugin_file_allowlist_matches_resources() -> None:
    from agent_gateway.paths import packaged_agw_claude_plugin_files

    assert set(packaged_agw_claude_plugin_files()) == _PLUGIN_FILES


def test_install_rejects_relative_runtime_python(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("sys.executable", "bin/python")

    result = install_claude_usage({"HOME": str(tmp_path)})

    plugin_dir = tmp_path / ".claude" / "skills" / "agent-gateway"
    assert not plugin_dir.exists()
    assert not result.skill_path.exists()
    assert any("could not resolve AGW's runtime" in note for note in result.notes)
    assert any("disabled the managed /agw-usage" in note for note in result.notes)


def test_install_does_not_register_skill_if_plugin_install_raises(tmp_path, monkeypatch) -> None:
    def broken_install(*_args, **_kwargs):
        raise OSError("disk failure")

    monkeypatch.setattr(
        "agent_gateway.claude_integration._install_companion_plugin",
        broken_install,
    )

    result = install_claude_usage({"HOME": str(tmp_path)})

    assert result.skill_installed is False
    assert not result.skill_path.exists()
    assert any("handler could not be installed" in note for note in result.notes)


def test_install_preserves_runtime_venv_entry_path(tmp_path, monkeypatch) -> None:
    runtime = tmp_path / "runtime-python"
    runtime.write_text("#!/bin/sh\nexit 0\n")
    runtime.chmod(0o755)
    venv_python = tmp_path / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(runtime)
    monkeypatch.setattr("sys.executable", str(venv_python))

    install_claude_usage({"HOME": str(tmp_path)})

    plugin_dir = tmp_path / ".claude" / "skills" / "agent-gateway"
    hooks = json.loads((plugin_dir / "hooks" / "hooks.json").read_text())
    command = hooks["hooks"]["UserPromptExpansion"][0]["hooks"][0]["command"]
    assert command == str(venv_python)


def test_runtime_command_ignores_project_shadow_package(tmp_path) -> None:
    import subprocess
    import sys

    shadow = tmp_path / "agent_gateway"
    shadow.mkdir()
    (shadow / "__init__.py").write_text("")
    (shadow / "__main__.py").write_text("raise SystemExit('project shadow executed')\n")

    result = subprocess.run(
        [sys.executable, "-I", "-m", "agent_gateway", "--version"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.startswith("agw ")
    assert "project shadow executed" not in result.stderr
