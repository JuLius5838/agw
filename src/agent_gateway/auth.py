"""Authentication orchestration: stage, probe, then atomically swap credentials.

The safety contract (independent of any specific provider):
  * A device flow authenticates into a fresh *staging* directory, never the active
    one, under a restrictive umask.
  * Only after a bounded probe succeeds are credentials atomically swapped into the
    active directory.
  * Cancellation or any failure removes staging and leaves the previous valid
    credentials byte-for-byte intact.
  * Device authorization requires a controlling TTY.
  * An already-authenticated provider returns success without a new device flow
    (unless ``force`` is set).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path

from agent_gateway.errors import AuthError
from agent_gateway.paths import DIR_MODE, SECRET_FILE_MODE, Paths, chmod_if_posix, ensure_dir
from agent_gateway.providers import Provider
from agent_gateway.providers.base import AuthState, AuthStatus, ProviderAdapter
from agent_gateway.providers.chatgpt import ChatGPTAdapter


def all_adapters() -> tuple[ProviderAdapter, ...]:
    return (ChatGPTAdapter(),)


def get_adapter(provider: Provider) -> ProviderAdapter:
    if provider is Provider.chatgpt:
        return ChatGPTAdapter()
    raise AuthError(f"unknown provider: {provider}")  # pragma: no cover - enum-exhaustive


def provider_process_env(paths: Paths) -> dict[str, str]:
    """Token-directory environment for the managed daemon (points at active creds).

    These variables are scoped to the managed LiteLLM process only — never added
    to Claude Code's child environment.
    """
    env: dict[str, str] = {}
    for adapter in all_adapters():
        env.update(adapter.process_env(adapter.active_token_dir(paths)))
    return env


@contextlib.contextmanager
def scoped_env(overrides: Mapping[str, str]) -> Iterator[None]:
    """Temporarily apply ``overrides`` to ``os.environ``, restoring on exit."""
    previous = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _staging_dir(adapter: ProviderAdapter, paths: Paths) -> Path:
    active = adapter.active_token_dir(paths)
    return active.with_name(f".{active.name}.staging")


def _rmtree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def _secure_tree(root: Path) -> None:
    """Clamp a whole credential tree to 0700 dirs / 0600 files on POSIX."""
    chmod_if_posix(root, DIR_MODE)
    for child in root.rglob("*"):
        chmod_if_posix(child, DIR_MODE if child.is_dir() else SECRET_FILE_MODE)


def _atomic_swap_dir(staging: Path, active: Path) -> None:
    """Atomically replace ``active`` with ``staging``, restoring on failure."""
    ensure_dir(active.parent)
    backup = active.with_name(f".{active.name}.bak")
    _rmtree(backup)
    if active.exists():
        os.rename(active, backup)
    try:
        os.rename(staging, active)
    except BaseException:
        # Restore the previous active credentials if the swap failed mid-way.
        if backup.exists() and not active.exists():
            os.rename(backup, active)
        raise
    _rmtree(backup)


def authenticate(
    paths: Paths,
    adapter: ProviderAdapter,
    *,
    model: str | None = None,
    force: bool = False,
    isatty: bool | None = None,
) -> AuthState:
    """Authenticate ``adapter`` and return the resulting active :class:`AuthState`."""
    if isatty is None:
        isatty = sys.stdin.isatty()

    current = adapter.auth_state(paths)
    if current.status is AuthStatus.authenticated and not force:
        return current

    if not isatty:
        raise AuthError(
            "device authorization requires an interactive terminal (TTY).",
            hint=adapter.remediation(),
        )

    staging = _staging_dir(adapter, paths)
    _rmtree(staging)
    ensure_dir(staging)

    previous_umask = os.umask(0o077)
    try:
        with scoped_env(adapter.process_env(staging)):
            adapter.run_device_flow(staging, model=model)
        _secure_tree(staging)
        adapter.probe_staged(staging)
        _atomic_swap_dir(staging, adapter.active_token_dir(paths))
    except BaseException:
        # Cancellation or failure: drop staging, keep any prior valid credentials.
        _rmtree(staging)
        raise
    finally:
        os.umask(previous_umask)

    return adapter.auth_state(paths)
