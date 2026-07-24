"""``agw uninstall``: stop the managed proxy and remove generated runtime files.

Credentials are preserved unless ``--credentials`` is given *and* separately
acknowledged (interactive confirmation, or ``--yes`` for non-interactive use).
Uninstall never removes the native Claude binary and never touches shell startup
files beyond the managed block (removed via the shell module when integration was
enabled).
"""

from __future__ import annotations

import contextlib
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from agent_gateway import proxy
from agent_gateway.claude_integration import uninstall_claude_usage
from agent_gateway.config import GatewayConfig, load_config
from agent_gateway.errors import ConfigError
from agent_gateway.paths import Paths
from agent_gateway.shell import disable as shell_disable


@dataclass
class UninstallResult:
    proxy_stopped: bool
    removed: list[str] = field(default_factory=list)
    credentials_removed: bool = False
    notes: list[str] = field(default_factory=list)


def _remove(path: Path, removed: list[str]) -> None:
    if path.is_dir():
        shutil.rmtree(path)
        removed.append(str(path))
    elif path.exists():
        path.unlink()
        removed.append(str(path))


def run_uninstall(
    paths: Paths,
    *,
    credentials: bool = False,
    acknowledged: bool = False,
    env: Mapping[str, str] | None = None,
) -> UninstallResult:
    # Credential deletion requires an explicit, separate acknowledgement (an
    # interactive confirmation or `--yes`), resolved by the caller.
    if credentials and not acknowledged:
        raise ConfigError(
            "refusing to delete credentials without confirmation.",
            hint="Re-run with `--credentials --yes` to acknowledge non-interactively.",
        )

    stopped = False
    with contextlib.suppress(Exception):
        stopped = proxy.stop(paths)

    # Remove the managed shell block if integration was enabled.
    notes: list[str] = []
    try:
        config: GatewayConfig | None = load_config(paths)
    except ConfigError:
        config = None
    if config and config.shell_integration:
        with contextlib.suppress(Exception):
            result = shell_disable(paths, config, config.shell_integration)
            notes.append(result.message)

    configured_claude_dir = (
        Path(config.claude_config_dir) if config and config.claude_config_dir else None
    )
    notes.extend(uninstall_claude_usage(env, claude_dir=configured_claude_dir))

    removed: list[str] = []
    # Generated runtime state (never credentials).
    _remove(paths.proxy_state_file, removed)
    _remove(paths.proxy_lock_file, removed)
    _remove(paths.generated_litellm_config, removed)
    _remove(paths.logs_dir, removed)
    _remove(paths.usage_dir, removed)
    _remove(paths.shell_dir, removed)
    _remove(paths.config_file, removed)
    _remove(paths.models_file, removed)

    credentials_removed = False
    if credentials:
        _remove(paths.credentials_dir, removed)
        credentials_removed = True
    else:
        notes.append(f"kept credentials at {paths.credentials_dir} (use --credentials to remove)")

    return UninstallResult(
        proxy_stopped=stopped,
        removed=removed,
        credentials_removed=credentials_removed,
        notes=notes,
    )
