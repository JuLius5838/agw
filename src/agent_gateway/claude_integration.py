"""Install the standalone Claude ``/agw-usage`` skill and usage collector."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shlex
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from agent_gateway.paths import (
    SECRET_FILE_MODE,
    atomic_write_text,
    ensure_dir,
    packaged_agw_claude_plugin_files,
    packaged_agw_usage_skill,
)

_LEGACY_STATUSLINE_COMMAND = "agw capture-claude-usage"
_SKILL_OWNER = "agent-gateway"
_SKILL_MARKER = ".agw-owned.json"
_PLUGIN_DIRECTORY = "agent-gateway"
_PLUGIN_MARKER = ".agw-owned.json"
_STATUSLINE_MARKER = ".agw-statusline-owned.json"
_PLUGIN_FILES = frozenset(
    {
        ".claude-plugin/plugin.json",
        ".mcp.json",
        "hooks/hooks.json",
    }
)


@dataclass(frozen=True)
class ClaudeUsageInstallResult:
    """Files installed and non-destructive integration notes."""

    claude_dir: Path
    skill_path: Path
    skill_installed: bool
    collector_enabled: bool
    notes: list[str] = field(default_factory=list)


def _claude_dir(environ: Mapping[str, str]) -> Path:
    configured = environ.get("CLAUDE_CONFIG_DIR")
    if configured and Path(configured).expanduser().is_absolute():
        return Path(configured).expanduser()
    home = Path(environ.get("HOME") or os.path.expanduser("~"))
    return home / ".claude"


def _skill_paths(claude_dir: Path) -> tuple[Path, Path]:
    skill_dir = claude_dir / "skills" / "agw-usage"
    return skill_dir / "SKILL.md", skill_dir / _SKILL_MARKER


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _marker_hash(marker_path: Path) -> str | None:
    if not marker_path.is_file():
        return None
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(marker, dict) or marker.get("owner") != _SKILL_OWNER:
        return None
    digest = marker.get("sha256")
    return digest if isinstance(digest, str) else None


def _write_managed_skill(skill_path: Path, marker_path: Path, content: str) -> None:
    ensure_dir(skill_path.parent)
    atomic_write_text(skill_path, content, mode=SECRET_FILE_MODE)
    marker = {
        "owner": _SKILL_OWNER,
        "sha256": _content_hash(content),
    }
    atomic_write_text(
        marker_path,
        json.dumps(marker, sort_keys=True) + "\n",
        mode=SECRET_FILE_MODE,
    )


def _plugin_paths(claude_dir: Path) -> tuple[Path, Path]:
    plugin_dir = claude_dir / "skills" / _PLUGIN_DIRECTORY
    return plugin_dir, plugin_dir / _PLUGIN_MARKER


def _plugin_marker_hashes(marker_path: Path) -> dict[str, str]:
    if not marker_path.is_file() or marker_path.is_symlink():
        return {}
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(marker, dict) or marker.get("owner") != _SKILL_OWNER:
        return {}
    files = marker.get("files")
    if not isinstance(files, dict):
        return {}
    return {
        path: digest
        for path, digest in files.items()
        if path in _PLUGIN_FILES and isinstance(digest, str)
    }


def _plugin_destination(plugin_dir: Path, relative: str) -> Path | None:
    """Resolve a packaged plugin path without following plugin-owned symlinks."""
    if relative not in _PLUGIN_FILES:
        return None
    destination = plugin_dir / relative
    current = plugin_dir
    for component in Path(relative).parts[:-1]:
        current /= component
        if current.is_symlink():
            return None
    try:
        destination.resolve(strict=False).relative_to(plugin_dir.resolve(strict=False))
    except (OSError, ValueError):
        return None
    return destination


def _resolve_runtime_python() -> str | None:
    """Return the interpreter already running this trusted AGW installation."""
    candidate = Path(sys.executable).expanduser()
    if not candidate.is_absolute():
        return None
    absolute = Path(os.path.abspath(candidate))
    try:
        resolved = absolute.resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        return None
    # Preserve the venv entry path: resolving its final symlink can bypass the
    # environment and make ``python -m agent_gateway`` import the wrong install.
    return str(absolute)


def _install_companion_plugin(
    claude_dir: Path,
    notes: list[str],
    *,
    executable: str | None = None,
) -> bool:
    """Install the isolated hook/MCP plugin without overwriting user files."""
    plugin_dir, marker_path = _plugin_paths(claude_dir)
    if plugin_dir.is_symlink() or marker_path.is_symlink():
        notes.append(
            f"kept symlink-managed Claude plugin at {plugin_dir}; "
            "the zero-credit usage dialog was not installed"
        )
        return False

    executable = executable or _resolve_runtime_python()
    if executable is None:
        notes.append(
            "could not resolve AGW's runtime interpreter; "
            "the zero-credit usage dialog was not installed"
        )
        return False
    escaped_executable = json.dumps(executable)[1:-1]
    packaged = {
        relative: (
            content.replace("__AGW_PYTHON_EXECUTABLE__", escaped_executable)
            .replace("__AGW_MODULE__", "agent_gateway")
        )
        for relative, content in packaged_agw_claude_plugin_files().items()
    }
    recorded = _plugin_marker_hashes(marker_path)
    owned: dict[str, str] = {}
    conflicts: list[Path] = []
    for relative, content in packaged.items():
        destination = _plugin_destination(plugin_dir, relative)
        if destination is None:
            conflicts.append(plugin_dir / relative)
            continue
        digest = _content_hash(content)
        if destination.is_symlink():
            conflicts.append(destination)
            if relative in recorded:
                owned[relative] = recorded[relative]
            continue
        if destination.is_file():
            try:
                current = destination.read_text(encoding="utf-8")
            except OSError:
                conflicts.append(destination)
                if relative in recorded:
                    owned[relative] = recorded[relative]
                continue
            if current != content and recorded.get(relative) != _content_hash(current):
                conflicts.append(destination)
                if relative in recorded:
                    owned[relative] = recorded[relative]
                continue
        ensure_dir(destination.parent)
        atomic_write_text(destination, content, mode=SECRET_FILE_MODE)
        owned[relative] = digest

    if owned:
        marker = {
            "owner": _SKILL_OWNER,
            "files": owned,
        }
        atomic_write_text(
            marker_path,
            json.dumps(marker, sort_keys=True) + "\n",
            mode=SECRET_FILE_MODE,
        )

    if conflicts:
        joined = ", ".join(str(path) for path in conflicts)
        notes.append(
            f"kept modified or unrelated Claude plugin files at {joined}; "
            "the zero-credit usage dialog is incomplete"
        )
        return False
    notes.append(
        "installed the local Claude usage dialog; restart Claude Code before "
        "running /agw-usage"
    )
    return True


def _uninstall_companion_plugin(claude_dir: Path, notes: list[str]) -> None:
    plugin_dir, marker_path = _plugin_paths(claude_dir)
    if plugin_dir.is_symlink() or marker_path.is_symlink():
        notes.append(f"kept symlink-managed Claude plugin at {plugin_dir}")
        return
    recorded = _plugin_marker_hashes(marker_path)
    if not recorded:
        return
    modified = False
    for relative, digest in recorded.items():
        destination = _plugin_destination(plugin_dir, relative)
        if destination is None:
            modified = True
            continue
        if destination.is_symlink():
            modified = True
            continue
        if not destination.is_file():
            continue
        try:
            current_digest = _content_hash(destination.read_text(encoding="utf-8"))
        except OSError:
            modified = True
            continue
        if current_digest != digest:
            modified = True
            continue
        destination.unlink()
    if modified:
        notes.append(f"kept modified Claude plugin files at {plugin_dir}")
        return
    marker_path.unlink(missing_ok=True)
    for directory in (
        plugin_dir / "hooks",
        plugin_dir / ".claude-plugin",
        plugin_dir,
    ):
        with contextlib.suppress(OSError):
            directory.rmdir()
    notes.append(f"removed managed Claude usage dialog from {plugin_dir}")


def _managed_statusline(executable: str) -> dict[str, object]:
    return {
        "type": "command",
        "command": (
            f"{shlex.quote(executable)} -I -m agent_gateway capture-claude-usage"
        ),
        "padding": 0,
    }


def _legacy_statusline() -> dict[str, object]:
    return {
        "type": "command",
        "command": _LEGACY_STATUSLINE_COMMAND,
        "padding": 0,
    }


def _statusline_marker_path(claude_dir: Path) -> Path:
    return claude_dir / _STATUSLINE_MARKER


def _statusline_marker_command(marker_path: Path) -> str | None:
    if not marker_path.is_file() or marker_path.is_symlink():
        return None
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(marker, dict) or marker.get("owner") != _SKILL_OWNER:
        return None
    command = marker.get("command")
    return command if isinstance(command, str) and command else None


def _record_statusline_command(marker_path: Path, command: str) -> bool:
    """Persist the exact generated command without replacing unrelated state."""
    if marker_path.is_symlink():
        return False
    if marker_path.exists() and _statusline_marker_command(marker_path) is None:
        return False
    ensure_dir(marker_path.parent)
    atomic_write_text(
        marker_path,
        json.dumps({"owner": _SKILL_OWNER, "command": command}, sort_keys=True) + "\n",
        mode=SECRET_FILE_MODE,
    )
    return True


def install_claude_usage(
    env: Mapping[str, str] | None = None,
) -> ClaudeUsageInstallResult:
    """Install ``/agw-usage`` and capture Claude limits when safe to do so.

    Existing custom status-line commands and unrelated skills are never replaced.
    An ownership marker lets AGW update only an unmodified skill it installed.
    """
    environ = os.environ if env is None else env
    claude_dir = _claude_dir(environ)
    skill_path, marker_path = _skill_paths(claude_dir)
    skill_content = packaged_agw_usage_skill()
    skill_installed = False
    skill_eligible = False
    skill_was_unowned = skill_path.is_file() and _marker_hash(marker_path) is None
    runtime_python = _resolve_runtime_python()
    notes: list[str] = []

    if skill_path.is_symlink() or marker_path.is_symlink():
        notes.append(
            f"kept symlink-managed Claude skill at {skill_path}; /agw-usage was not installed"
        )
    elif skill_path.is_file():
        try:
            current_content = skill_path.read_text(encoding="utf-8")
        except OSError:
            current_content = ""
        recorded_hash = _marker_hash(marker_path)
        unmodified_owned = (
            recorded_hash is not None and _content_hash(current_content) == recorded_hash
        )
        if unmodified_owned or current_content == skill_content:
            skill_eligible = True
        else:
            notes.append(
                f"kept unrelated or modified Claude skill at {skill_path}; "
                "/agw-usage was not installed"
            )
    else:
        skill_eligible = True

    if skill_eligible:
        try:
            plugin_installed = _install_companion_plugin(
                claude_dir,
                notes,
                executable=runtime_python,
            )
        except Exception:  # noqa: BLE001 - never register a command without its guard
            plugin_installed = False
            notes.append(
                "the local Claude usage handler could not be installed; "
                "the managed command was not registered"
            )
        if plugin_installed:
            _write_managed_skill(skill_path, marker_path, skill_content)
            skill_installed = True
            notes.append(f"installed Claude command /agw-usage at {skill_path}")
        else:
            if not skill_was_unowned:
                skill_path.unlink(missing_ok=True)
                with contextlib.suppress(OSError):
                    skill_path.parent.rmdir()
            marker_path.unlink(missing_ok=True)
            skill_installed = False
            notes.append(
                "disabled the managed /agw-usage command because its local "
                "zero-credit handler is incomplete"
            )

    settings_path = claude_dir / "settings.json"
    if settings_path.is_symlink():
        notes.append(
            f"kept symlink-managed Claude settings at {settings_path}; "
            "Claude usage capture was not enabled"
        )
        return ClaudeUsageInstallResult(
            claude_dir,
            skill_path,
            skill_installed,
            False,
            notes,
        )
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            notes.append(
                f"kept unreadable Claude settings at {settings_path}; "
                "Claude usage capture was not enabled"
            )
            return ClaudeUsageInstallResult(
                claude_dir,
                skill_path,
                skill_installed,
                False,
                notes,
            )
        if not isinstance(settings, dict):
            notes.append(
                f"kept non-object Claude settings at {settings_path}; "
                "Claude usage capture was not enabled"
            )
            return ClaudeUsageInstallResult(
                claude_dir,
                skill_path,
                skill_installed,
                False,
                notes,
            )
    else:
        settings = {}

    current = settings.get("statusLine")
    managed = _managed_statusline(runtime_python) if runtime_python is not None else None
    statusline_marker = _statusline_marker_path(claude_dir)
    recorded_statusline = _statusline_marker_command(statusline_marker)
    recorded_managed = (
        {
            "type": "command",
            "command": recorded_statusline,
            "padding": 0,
        }
        if recorded_statusline is not None
        else None
    )
    if current is None and managed is not None:
        command = managed["command"]
        assert isinstance(command, str)
        if not _record_statusline_command(statusline_marker, command):
            notes.append(
                f"kept unrelated status-line ownership state at {statusline_marker}; "
                "Claude usage capture was not enabled"
            )
            return ClaudeUsageInstallResult(
                claude_dir,
                skill_path,
                skill_installed,
                False,
                notes,
            )
        settings["statusLine"] = managed
        ensure_dir(settings_path.parent)
        atomic_write_text(
            settings_path,
            json.dumps(settings, indent=2, sort_keys=True) + "\n",
            mode=SECRET_FILE_MODE,
        )
        notes.append("enabled private Claude usage capture through the status line")
        return ClaudeUsageInstallResult(claude_dir, skill_path, skill_installed, True, notes)

    if managed is not None and current in (
        managed,
        _legacy_statusline(),
        recorded_managed,
    ):
        command = managed["command"]
        assert isinstance(command, str)
        if not _record_statusline_command(statusline_marker, command):
            notes.append(
                f"kept unrelated status-line ownership state at {statusline_marker}; "
                "Claude usage capture was not upgraded"
            )
            return ClaudeUsageInstallResult(
                claude_dir,
                skill_path,
                skill_installed,
                False,
                notes,
            )
        if current != managed:
            settings["statusLine"] = managed
            atomic_write_text(
                settings_path,
                json.dumps(settings, indent=2, sort_keys=True) + "\n",
                mode=SECRET_FILE_MODE,
            )
            notes.append("upgraded Claude usage capture to the pinned AGW runtime")
            return ClaudeUsageInstallResult(
                claude_dir,
                skill_path,
                skill_installed,
                True,
                notes,
            )
        notes.append("kept existing AGW Claude usage capture")
        return ClaudeUsageInstallResult(claude_dir, skill_path, skill_installed, True, notes)

    if current is None:
        notes.append(
            "could not resolve AGW's runtime interpreter; "
            "Claude usage capture was not enabled"
        )
        return ClaudeUsageInstallResult(
            claude_dir,
            skill_path,
            skill_installed,
            False,
            notes,
        )

    notes.append(
        "kept the existing custom Claude status line; Codex usage will work, "
        "but Claude limits need manual status-line integration"
    )
    return ClaudeUsageInstallResult(claude_dir, skill_path, skill_installed, False, notes)


def uninstall_claude_usage(
    env: Mapping[str, str] | None = None,
    *,
    claude_dir: Path | None = None,
) -> list[str]:
    """Remove only unmodified AGW-owned Claude usage integration artifacts."""
    environ = os.environ if env is None else env
    target_dir = claude_dir or _claude_dir(environ)
    skill_path, marker_path = _skill_paths(target_dir)
    statusline_marker = _statusline_marker_path(target_dir)
    recorded_statusline = _statusline_marker_command(statusline_marker)
    notes: list[str] = []

    _uninstall_companion_plugin(target_dir, notes)

    if skill_path.is_symlink() or marker_path.is_symlink():
        notes.append(f"kept symlink-managed Claude skill at {skill_path}")
    elif skill_path.is_file():
        try:
            current_content = skill_path.read_text(encoding="utf-8")
        except OSError:
            current_content = ""
        recorded_hash = _marker_hash(marker_path)
        if recorded_hash is not None and _content_hash(current_content) == recorded_hash:
            skill_path.unlink()
            marker_path.unlink(missing_ok=True)
            notes.append(f"removed managed Claude command /agw-usage from {skill_path}")
        else:
            notes.append(f"kept modified or unrelated Claude skill at {skill_path}")
    elif _marker_hash(marker_path) is not None:
        marker_path.unlink(missing_ok=True)

    settings_path = target_dir / "settings.json"
    if settings_path.is_symlink():
        notes.append(f"kept symlink-managed Claude settings at {settings_path}")
        return notes
    if not settings_path.is_file():
        return notes
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        notes.append(f"kept unreadable Claude settings at {settings_path}")
        return notes
    if not isinstance(settings, dict):
        notes.append(f"kept non-object Claude settings at {settings_path}")
        return notes
    managed_statuslines: list[dict[str, object]] = []
    if recorded_statusline is not None:
        managed_statuslines.append(
            {
                "type": "command",
                "command": recorded_statusline,
                "padding": 0,
            }
        )
    if managed_statuslines and settings.get("statusLine") in managed_statuslines:
        settings.pop("statusLine")
        atomic_write_text(
            settings_path,
            json.dumps(settings, indent=2, sort_keys=True) + "\n",
            mode=SECRET_FILE_MODE,
        )
        notes.append("removed managed Claude usage capture")
    if recorded_statusline is not None and not statusline_marker.is_symlink():
        statusline_marker.unlink(missing_ok=True)
    return notes
