"""Contract tests for the Claude Code plugin/marketplace manifests and skills.

Validates the manifests both structurally (JSON shape our release process
depends on — matching versions, the exact ``"./"`` marketplace source, skill
frontmatter) and, when the ``claude`` CLI is present, via ``claude plugin
validate .``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_JSON = REPO_ROOT / ".claude-plugin" / "plugin.json"
MARKETPLACE_JSON = REPO_ROOT / ".claude-plugin" / "marketplace.json"
SKILLS_DIR = REPO_ROOT / "skills"
EXPECTED_SKILLS = {"gateway-setup", "gateway-doctor", "model-routing", "gateway-add-model"}


def _package_version() -> str:
    from agent_gateway import __version__

    return __version__


def test_plugin_manifest_shape() -> None:
    data = json.loads(PLUGIN_JSON.read_text())
    assert data["name"] == "agent-gateway"
    assert data["version"] == _package_version()  # plugin/runtime versions match
    assert data["description"]


def test_marketplace_uses_local_source() -> None:
    data = json.loads(MARKETPLACE_JSON.read_text())
    assert data["name"] == "agent-gateway"
    entries = {p["name"]: p for p in data["plugins"]}
    assert "agent-gateway" in entries
    # The marketplace ships the plugin from the repo root.
    assert entries["agent-gateway"]["source"] == "./"


def test_public_manifests_advertise_only_shipped_providers() -> None:
    plugin = json.loads(PLUGIN_JSON.read_text())
    marketplace = json.loads(MARKETPLACE_JSON.read_text())
    entry = next(item for item in marketplace["plugins"] if item["name"] == "agent-gateway")

    for manifest in (plugin, entry):
        claims = f"{manifest['description']} {' '.join(manifest['keywords'])}".lower()
        assert "chatgpt" in claims or "codex" in claims
        assert "copilot" not in claims


def test_all_skills_present_with_frontmatter() -> None:
    found = {p.parent.name for p in SKILLS_DIR.glob("*/SKILL.md")}
    assert found >= EXPECTED_SKILLS, f"missing skills: {EXPECTED_SKILLS - found}"
    for skill in EXPECTED_SKILLS:
        text = (SKILLS_DIR / skill / "SKILL.md").read_text()
        assert text.startswith("---"), f"{skill} missing YAML frontmatter"
        assert f"name: {skill}" in text
        assert "description:" in text


def test_packaged_standalone_usage_skill_has_exact_command_name() -> None:
    from agent_gateway.paths import packaged_agw_usage_skill

    text = packaged_agw_usage_skill()
    assert text.startswith("---")
    assert "name: agw-usage" in text
    assert "disable-model-invocation: true" in text
    assert "handled locally" in text
    assert "allowed-tools" not in text


def test_usage_ui_plugin_is_hook_driven_not_model_driven() -> None:
    from agent_gateway.paths import (
        packaged_agw_claude_plugin_files,
        packaged_agw_usage_skill,
    )

    packaged = packaged_agw_claude_plugin_files()
    mcp = json.loads(packaged[".mcp.json"])
    server = mcp["mcpServers"]["usage-ui"]
    assert server == {
        "command": "__AGW_PYTHON_EXECUTABLE__",
        "args": ["-I", "-m", "__AGW_MODULE__", "claude-extension-server"],
    }

    hooks = json.loads(packaged["hooks/hooks.json"])
    groups = hooks["hooks"]["UserPromptExpansion"]
    exact = next(group for group in groups if group["matcher"] == "agw-usage")
    assert {hook["type"] for hook in exact["hooks"]} == {"command", "mcp_tool"}
    command_hook = next(hook for hook in exact["hooks"] if hook["type"] == "command")
    assert command_hook == {
        "type": "command",
        "command": "__AGW_PYTHON_EXECUTABLE__",
        "args": ["-I", "-m", "__AGW_MODULE__", "claude-usage-guard"],
        "timeout": 330,
    }
    mcp_hook = next(hook for hook in exact["hooks"] if hook["type"] == "mcp_tool")
    assert mcp_hook == {
        "type": "mcp_tool",
        "server": "plugin:agent-gateway:usage-ui",
        "tool": "show_usage",
        "timeout": 330,
        "input": {"session_id": "${session_id}"},
    }

    skill = packaged_agw_usage_skill()
    assert "disable-model-invocation: true" in skill


def test_model_routing_skill_forbids_aliases_and_fallback() -> None:
    text = (SKILLS_DIR / "model-routing" / "SKILL.md").read_text().lower()
    assert "agw models list --json" in text  # obtains exact names
    assert "verbatim" in text or "unchanged" in text  # passes name through
    assert "never invent" in text or "no invented" in text
    assert "spawn" in text and "fixed at spawn" in text
    assert "substitut" in text  # addresses allowlist substitution


def test_bootstrap_is_executable_and_ownership_aware() -> None:
    bootstrap = REPO_ROOT / "scripts" / "bootstrap.sh"
    text = bootstrap.read_text()
    assert "command -v agw" in text  # ownership preflight
    assert "uv tool install" in text
    assert "--refresh-package agent-gateway" in text
    assert "--python 3.12" in text
    assert "agw setup" in text


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not available")
def test_claude_plugin_validate() -> None:
    result = subprocess.run(
        ["claude", "plugin", "validate", "."],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"validate failed:\n{result.stdout}\n{result.stderr}"
