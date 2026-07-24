"""Per-user runtime paths and low-level, permission-safe filesystem helpers.

Everything the gateway writes lives below XDG-appropriate user directories:

    config: $XDG_CONFIG_HOME/agent-gateway   (default ~/.config/agent-gateway)
    state:  $XDG_STATE_HOME/agent-gateway     (default ~/.local/state/agent-gateway)
    logs:   <state>/logs

Config, credential, and state directories are created ``0700`` and secret files
``0600`` on POSIX platforms. Paths are computed from an injectable environment
mapping so tests can run entirely inside a temporary HOME/XDG tree with no global
state.
"""

from __future__ import annotations

import contextlib
import importlib.resources as importlib_resources
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

APP_DIR_NAME = "agent-gateway"

_IS_POSIX = os.name == "posix"

# Default restrictive permissions (POSIX only).
DIR_MODE = 0o700
SECRET_FILE_MODE = 0o600


@dataclass(frozen=True)
class Paths:
    """Resolved absolute locations for all gateway runtime files."""

    config_dir: Path
    state_dir: Path

    # --- config directory ---
    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.yaml"

    @property
    def models_file(self) -> Path:
        return self.config_dir / "models.yaml"

    @property
    def credentials_dir(self) -> Path:
        return self.config_dir / "credentials"

    @property
    def proxy_key_file(self) -> Path:
        return self.credentials_dir / "proxy-key"

    def provider_credentials_dir(self, provider: str) -> Path:
        return self.credentials_dir / provider

    @property
    def shell_dir(self) -> Path:
        return self.config_dir / "shell"

    def shell_source_file(self, shell: str) -> Path:
        return self.shell_dir / f"agw.{shell}"

    # --- state directory ---
    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def proxy_state_file(self) -> Path:
        return self.state_dir / "proxy.json"

    @property
    def proxy_lock_file(self) -> Path:
        return self.state_dir / "proxy.lock"

    @property
    def generated_litellm_config(self) -> Path:
        return self.state_dir / "generated-litellm.yaml"

    @property
    def proxy_log(self) -> Path:
        return self.logs_dir / "proxy.log"

    @property
    def usage_dir(self) -> Path:
        return self.state_dir / "usage"

    @property
    def claude_usage_file(self) -> Path:
        return self.usage_dir / "claude.json"


def _xdg_base(env: Mapping[str, str], var: str, default_root: Path) -> Path:
    """Resolve an XDG base dir, honoring the spec: a relative value is ignored."""
    value = env.get(var)
    if value and os.path.isabs(value):
        return Path(value) / APP_DIR_NAME
    return default_root / APP_DIR_NAME


def get_paths(env: Mapping[str, str] | None = None) -> Paths:
    """Compute runtime paths from ``env`` (defaults to the live process environment)."""
    environ: Mapping[str, str] = os.environ if env is None else env
    home = Path(environ.get("HOME") or os.path.expanduser("~"))
    config_dir = _xdg_base(environ, "XDG_CONFIG_HOME", home / ".config")
    state_dir = _xdg_base(environ, "XDG_STATE_HOME", home / ".local" / "state")
    return Paths(config_dir=config_dir, state_dir=state_dir)


# --------------------------------------------------------------------------- #
# Filesystem primitives (permission-safe, atomic)
# --------------------------------------------------------------------------- #
def chmod_if_posix(path: Path, mode: int) -> None:
    if _IS_POSIX:
        os.chmod(path, mode)


def ensure_dir(path: Path, mode: int = DIR_MODE) -> Path:
    """Create ``path`` (and parents) and clamp it to ``mode`` on POSIX."""
    path.mkdir(parents=True, exist_ok=True)
    chmod_if_posix(path, mode)
    return path


def atomic_write_text(path: Path, content: str, *, mode: int = SECRET_FILE_MODE) -> None:
    """Write ``content`` to ``path`` atomically with restrictive permissions.

    The temporary file is created in the same directory (so ``os.replace`` is a
    same-filesystem rename) and is fchmod-ed *before* any content is written, so a
    secret never briefly exists as a world-readable file.
    """
    directory = path.parent
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        if _IS_POSIX:
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise
    chmod_if_posix(path, mode)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Packaged resources
# --------------------------------------------------------------------------- #
def packaged_default_models_yaml() -> str:
    """Return the shipped, credential-free default model registry as text."""
    resource = importlib_resources.files("agent_gateway.resources") / "default-models.yaml"
    return resource.read_text(encoding="utf-8")


def packaged_shell_snippet(shell: str) -> str:
    """Return the shipped shell integration snippet for ``zsh`` or ``bash``."""
    resource = importlib_resources.files("agent_gateway.resources") / f"shell.{shell}"
    return resource.read_text(encoding="utf-8")


def packaged_agw_usage_skill() -> str:
    """Return the standalone Claude Code ``/agw-usage`` skill."""
    resource = importlib_resources.files("agent_gateway.resources") / "agw-usage-skill.md"
    return resource.read_text(encoding="utf-8")


def packaged_agw_claude_plugin_files() -> dict[str, str]:
    """Return files for the personal skills-directory companion plugin."""
    resources = importlib_resources.files("agent_gateway.resources")
    return {
        ".claude-plugin/plugin.json": (resources / "agw-claude-plugin-manifest.json").read_text(
            encoding="utf-8"
        ),
        ".mcp.json": (resources / "agw-claude-plugin-mcp.json").read_text(encoding="utf-8"),
        "hooks/hooks.json": (resources / "agw-claude-plugin-hooks.json").read_text(
            encoding="utf-8"
        ),
    }
