"""User configuration (``config.yaml``): non-secret runtime choices.

The model registry (``models.yaml``) is handled separately in
:mod:`agent_gateway.model_registry`. This module owns the small set of scalar
user choices — proxy port, the resolved native Claude path, the agent-team
opt-in, and which shell (if any) has bare-``claude`` integration — plus helpers
to validate the native Claude executable and read (never write) Claude Code's
own settings so ``doctor`` can warn about model-allowlist conflicts.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_gateway.errors import ConfigError
from agent_gateway.paths import (
    SECRET_FILE_MODE,
    Paths,
    atomic_write_text,
    ensure_dir,
    read_text,
)

ShellName = Literal["zsh", "bash"]
EffortLevel = Literal["low", "medium", "high", "xhigh", "max"]

DEFAULT_PROXY_PORT = 4000


class GatewayConfig(BaseModel):
    """Non-secret, user-editable runtime configuration."""

    model_config = ConfigDict(extra="forbid")

    port: int = Field(default=DEFAULT_PROXY_PORT, ge=1, le=65535)
    native_claude_path: str | None = None
    agent_teams_enabled: bool = False
    shell_integration: ShellName | None = None
    default_effort: EffortLevel | None = None
    claude_config_dir: str | None = None
    codex_cli_path: str | None = None
    codex_cli_sha256: str | None = None


def load_config(paths: Paths) -> GatewayConfig:
    """Load and validate ``config.yaml``.

    Raises :class:`ConfigError` if the file is missing (setup not run) or invalid.
    """
    path = paths.config_file
    if not path.exists():
        raise ConfigError(
            "agent-gateway is not set up on this machine.",
            hint="Run `agw setup` first.",
        )
    try:
        raw = yaml.safe_load(read_text(path)) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"could not parse {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a top-level mapping.")
    try:
        return GatewayConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration in {path}: {exc}") from exc


def save_config(paths: Paths, config: GatewayConfig) -> None:
    """Persist ``config.yaml`` atomically with restrictive permissions."""
    ensure_dir(paths.config_dir)
    data = config.model_dump(exclude_none=True)
    text = yaml.safe_dump(data, sort_keys=True, default_flow_style=False)
    atomic_write_text(paths.config_file, text, mode=SECRET_FILE_MODE)


def validate_native_claude_path(candidate: str | os.PathLike[str]) -> Path:
    """Validate a native Claude executable path and return a stable absolute path.

    The returned path is made absolute but its *final* symlink is preserved: on a
    typical install ``~/.local/bin/claude`` is a symlink that Claude repoints on
    every auto-update, so storing the fully resolved (version-pinned) target would
    break after the next update. We therefore validate the resolved target (it
    must exist, be executable, and not be the gateway itself, which would recurse)
    but return the caller's stable path.
    """
    absolute = Path(os.path.abspath(Path(candidate).expanduser()))
    resolved = absolute.resolve()
    if not resolved.is_file():
        raise ConfigError(
            f"native Claude executable not found: {absolute}",
            hint="Install Claude Code, or pass the correct path.",
        )
    if not os.access(resolved, os.X_OK):
        raise ConfigError(f"native Claude path is not executable: {absolute}")
    if absolute.name in {"agw", "agent_gateway"} or resolved.name in {"agw", "agent_gateway"}:
        raise ConfigError(
            "native Claude path resolves to the gateway itself (would recurse).",
            hint="Point it at the real Claude binary, e.g. `command -v claude`.",
        )
    agw_path = shutil.which("agw")
    if agw_path and Path(agw_path).resolve() == resolved:
        raise ConfigError(
            "native Claude path resolves to `agw` (would recurse).",
            hint="Point it at the real Claude binary, e.g. `command -v claude`.",
        )
    return absolute


def discover_native_claude(env: dict[str, str] | None = None) -> Path | None:
    """Best-effort discovery of the native Claude executable on ``PATH``.

    Returns ``None`` if not found or if the only match resolves back to the
    gateway wrapper; callers should then ask the user for an explicit path.
    """
    environ = os.environ if env is None else env
    found = shutil.which("claude", path=environ.get("PATH"))
    if not found:
        return None
    try:
        return validate_native_claude_path(found)
    except ConfigError:
        return None


def executable_sha256(path: Path) -> str:
    """Hash an executable without loading it all into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover_codex_cli(
    env: Mapping[str, str] | None = None,
) -> tuple[Path, str] | None:
    """Resolve and fingerprint the Codex CLI selected during explicit setup."""
    environ = os.environ if env is None else env
    found = shutil.which("codex", path=environ.get("PATH"))
    if not found:
        return None
    try:
        resolved = Path(found).expanduser().resolve(strict=True)
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            return None
        return resolved, executable_sha256(resolved)
    except OSError:
        return None


def read_claude_settings(env: dict[str, str] | None = None) -> list[tuple[Path, dict[str, object]]]:
    """Read (never modify) inspectable Claude Code settings files.

    Returns a list of ``(path, parsed_json)`` for each settings file that exists
    and parses. Used by ``doctor``/``setup`` to warn when an ``availableModels``
    allowlist would exclude a configured gateway model. Managed or unreadable
    policy is simply absent from the result — it is a documented precondition,
    not something the gateway can bypass.
    """
    environ = os.environ if env is None else env
    home = Path(environ.get("HOME") or os.path.expanduser("~"))
    candidates = [
        home / ".claude" / "settings.json",
        Path.cwd() / ".claude" / "settings.json",
        Path.cwd() / ".claude" / "settings.local.json",
    ]
    results: list[tuple[Path, dict[str, object]]] = []
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            parsed = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            results.append((candidate, parsed))
    return results
