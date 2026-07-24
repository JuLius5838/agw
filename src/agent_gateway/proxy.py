"""Managed hybrid-router lifecycle.

``ensure_running`` reuses a healthy managed proxy or starts one, serialized by a
file lock so concurrent callers converge on a single daemon. The public child is
always the AGW router; a private LiteLLM child exists only when at least one
external model is active. A healthy runtime with a different fingerprint is never
restarted implicitly (that needs an explicit ``agw proxy restart``).
"""

from __future__ import annotations

import contextlib
import json
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import Enum, auto
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import cast

import httpx
from filelock import FileLock, Timeout

from agent_gateway import __version__ as _AGW_VERSION
from agent_gateway.config import GatewayConfig
from agent_gateway.errors import AuthError, PortConflictError, ProxyError
from agent_gateway.litellm_config import config_fingerprint, write_litellm_config
from agent_gateway.model_registry import ModelRegistry
from agent_gateway.paths import Paths, atomic_write_text, ensure_dir, read_text
from agent_gateway.process import ProcessIdentity, find_free_port, is_alive, is_port_open, terminate
from agent_gateway.providers.base import AuthStatus
from agent_gateway.secret_store import ensure_proxy_key

HOST = "127.0.0.1"
STATE_SCHEMA = 2
# Bump whenever the managed process composition or launch contract changes.
# This makes editable/local upgrades fail closed instead of silently reusing a
# daemon whose package version happens to be unchanged.
RUNTIME_ABI = "5-litellm-readiness"
LOCK_TIMEOUT_SECONDS = 30.0
READINESS_TIMEOUT_SECONDS = 30.0
_READINESS_POLL_SECONDS = 0.25


def litellm_version() -> str:
    try:
        return _pkg_version("litellm")
    except Exception:  # noqa: BLE001 - version is best-effort metadata
        return "unknown"


def runtime_fingerprint(rendered_config: str) -> str:
    """Fingerprint over runtime ABI + package/dependency versions + config.

    A change here means new launches must not silently reuse or restart a proxy
    started under the old fingerprint (EC-13).
    """
    material = (
        f"runtime_abi={RUNTIME_ABI}\n"
        f"agw={_AGW_VERSION}\n"
        f"litellm={litellm_version()}\n"
        f"config={config_fingerprint(rendered_config)}"
    )
    return config_fingerprint(material)


@dataclass(frozen=True)
class ProxyState:
    """Persisted identity/address of a managed proxy (never contains secrets)."""

    supervisor: ProcessIdentity
    router: ProcessIdentity
    litellm: ProcessIdentity | None
    host: str
    port: int
    litellm_host: str | None
    litellm_port: int | None
    litellm_version: str
    config_fingerprint: str
    runtime_fingerprint: str
    log_path: str

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": STATE_SCHEMA,
            "supervisor": self.supervisor.to_dict(),
            "router": self.router.to_dict(),
            "litellm": self.litellm.to_dict() if self.litellm is not None else None,
            "host": self.host,
            "port": self.port,
            "litellm_host": self.litellm_host,
            "litellm_port": self.litellm_port,
            "litellm_version": self.litellm_version,
            "config_fingerprint": self.config_fingerprint,
            "runtime_fingerprint": self.runtime_fingerprint,
            "log_path": self.log_path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ProxyState:
        litellm_data = data.get("litellm")
        return cls(
            supervisor=ProcessIdentity.from_dict(cast("dict[str, object]", data["supervisor"])),
            router=ProcessIdentity.from_dict(cast("dict[str, object]", data["router"])),
            litellm=(
                ProcessIdentity.from_dict(cast("dict[str, object]", litellm_data))
                if isinstance(litellm_data, dict)
                else None
            ),
            host=str(data["host"]),
            port=int(str(data["port"])),
            litellm_host=(
                str(data["litellm_host"]) if data.get("litellm_host") is not None else None
            ),
            litellm_port=(
                int(str(data["litellm_port"])) if data.get("litellm_port") is not None else None
            ),
            litellm_version=str(data["litellm_version"]),
            config_fingerprint=str(data["config_fingerprint"]),
            runtime_fingerprint=str(data["runtime_fingerprint"]),
            log_path=str(data["log_path"]),
        )


def write_state(path: Path, state: ProxyState) -> None:
    ensure_dir(path.parent)
    atomic_write_text(path, json.dumps(state.to_dict(), indent=2) + "\n", mode=0o600)


def read_state(paths: Paths) -> ProxyState | None:
    path = paths.proxy_state_file
    if not path.is_file():
        return None
    try:
        data = json.loads(read_text(path))
        if not isinstance(data, dict) or data.get("schema") != STATE_SCHEMA:
            return None
        return ProxyState.from_dict(data)
    except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


def clear_state(paths: Paths) -> None:
    with contextlib.suppress(FileNotFoundError):
        paths.proxy_state_file.unlink()


def health_models(
    url: str, key: str, *, timeout: float = 2.0, attempts: int = 1
) -> set[str] | None:
    """Return the model-id set from an authenticated ``/v1/models``, or ``None``.

    Retries up to ``attempts`` times so a transient hiccup on a live proxy is not
    mistaken for an unhealthy one.
    """
    for attempt in range(attempts):
        try:
            response = httpx.get(
                f"{url}/v1/models",
                headers={"X-AGW-Key": key},
                timeout=timeout,
            )
            if response.status_code == 200:
                return {entry["id"] for entry in response.json().get("data", [])}
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            pass
        if attempt + 1 < attempts:
            time.sleep(0.3)
    return None


class _Reuse(Enum):
    OK = auto()
    DEAD = auto()
    FINGERPRINT_MISMATCH = auto()
    PORT_CONFLICT = auto()
    HUNG = auto()


def _classify(state: ProxyState, expected_fp: str) -> _Reuse:
    children_alive = is_alive(state.router) and (state.litellm is None or is_alive(state.litellm))
    if not (is_alive(state.supervisor) and children_alive):
        # Our processes are gone. If the port is held by something else it is a
        # conflict; otherwise it's just stale state we can safely clear.
        return _Reuse.PORT_CONFLICT if is_port_open(state.host, state.port) else _Reuse.DEAD

    # The managed processes are alive — never clear this state. A different
    # runtime/config fingerprint must not be reused or restarted implicitly.
    if state.runtime_fingerprint != expected_fp:
        return _Reuse.FINGERPRINT_MISMATCH

    # Reuse decision is intentionally HTTP-free: our own managed processes are
    # alive, the config fingerprint matches (so the served model set is exactly
    # what we rendered and verified at boot in `_await_ready`), and the socket is
    # accepting connections. Depending on a fresh `/v1/models` round-trip here
    # makes reuse flaky under CPU load (many cold launchers importing litellm),
    # without adding real safety beyond identity + fingerprint + listening.
    if is_port_open(state.host, state.port):
        return _Reuse.OK
    return _Reuse.HUNG  # ours, right config, but not accepting connections


def _require_authenticated_providers(paths: Paths, registry: ModelRegistry) -> None:
    """Fail fast if a provider backing an active model is not authenticated.

    Without this, LiteLLM would enter an interactive 15-minute device flow at
    startup and the readiness check would simply time out.
    """
    from agent_gateway.auth import get_adapter

    providers = {model.provider for model in registry.active_models()}
    for provider in sorted(providers, key=lambda p: p.value):
        adapter = get_adapter(provider)
        state = adapter.auth_state(paths)
        if state.status is not AuthStatus.authenticated:
            raise AuthError(
                f"{adapter.display_name} is not authenticated ({state.detail}).",
                hint=adapter.remediation(),
            )


def _litellm_executable() -> str:
    candidate = Path(sys.executable).parent / "agw-litellm"
    if not candidate.exists():
        raise ProxyError(
            "the AGW LiteLLM compatibility runner is not installed.",
            hint="Re-run the plugin bootstrap to reinstall agent-gateway.",
        )
    return str(candidate)


def _supervisor_env(paths: Paths, local_key: str, internal_key: str) -> dict[str, str]:
    import os

    from agent_gateway.auth import provider_process_env

    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "AGW_LOCAL_KEY": local_key,
        "AGW_LITELLM_KEY": internal_key,
        "LITELLM_MASTER_KEY": internal_key,
        "LITELLM_DONT_SHOW_FEEDBACK_BOX": "1",
    }
    env.update(provider_process_env(paths))
    return env


def _spawn_supervisor(
    paths: Paths,
    config: GatewayConfig,
    local_key: str,
    runtime_fp: str,
    *,
    with_litellm: bool,
) -> None:
    clear_state(paths)
    ensure_dir(paths.logs_dir)
    internal_key = secrets.token_urlsafe(32)
    litellm_port = find_free_port(HOST) if with_litellm else None
    args = [
        sys.executable,
        "-m",
        "agent_gateway.supervisor",
        "--host",
        HOST,
        "--port",
        str(config.port),
        "--models-file",
        str(paths.models_file),
        "--log-file",
        str(paths.proxy_log),
        "--state-file",
        str(paths.proxy_state_file),
        "--litellm-version",
        litellm_version(),
        "--config-fingerprint",
        config_fingerprint(read_text(paths.generated_litellm_config)),
        "--runtime-fingerprint",
        runtime_fp,
    ]
    if litellm_port is not None:
        args.extend(
            [
                "--litellm",
                _litellm_executable(),
                "--config",
                str(paths.generated_litellm_config),
                "--litellm-host",
                HOST,
                "--litellm-port",
                str(litellm_port),
            ]
        )
    subprocess.Popen(
        args,
        env=_supervisor_env(paths, local_key, internal_key),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _await_ready(paths: Paths, key: str, expected_models: set[str]) -> ProxyState:
    deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        state = read_state(paths)
        if state is not None:
            if not is_alive(state.supervisor):
                break  # supervisor died during startup
            models = health_models(state.url, key)
            internal_ready = state.litellm is None or (
                state.litellm_host is not None
                and state.litellm_port is not None
                and is_alive(state.litellm)
                and is_port_open(state.litellm_host, state.litellm_port)
            )
            if models == expected_models and internal_ready:
                return state
        time.sleep(_READINESS_POLL_SECONDS)

    # Not ready: tear down whatever we can and surface the log path.
    state = read_state(paths)
    if state is not None:
        with contextlib.suppress(Exception):
            terminate(state.router)
        with contextlib.suppress(Exception):
            if state.litellm is not None:
                terminate(state.litellm)
        with contextlib.suppress(Exception):
            terminate(state.supervisor)
    clear_state(paths)
    raise ProxyError(
        f"the proxy did not become ready within {int(READINESS_TIMEOUT_SECONDS)}s.",
        hint=f"Inspect {paths.proxy_log} or run `agw doctor`.",
    )


def _lock(paths: Paths) -> FileLock:
    ensure_dir(paths.state_dir)
    return FileLock(str(paths.proxy_lock_file))


def ensure_running(paths: Paths, config: GatewayConfig, registry: ModelRegistry) -> ProxyState:
    """Reuse a healthy managed proxy or start one; return its state."""
    key = ensure_proxy_key(paths)
    rendered = write_litellm_config(paths, registry)
    expected_fp = runtime_fingerprint(rendered)
    expected_models = {picker_id for picker_id, _name in registry.picker_models()}

    try:
        with _lock(paths).acquire(timeout=LOCK_TIMEOUT_SECONDS):
            existing = read_state(paths)
            if existing is not None:
                verdict = _classify(existing, expected_fp)
                if verdict is _Reuse.OK:
                    return existing
                if verdict is _Reuse.FINGERPRINT_MISMATCH:
                    raise ProxyError(
                        "a proxy is running with a different config/version.",
                        hint="Run `agw proxy restart` to apply the change.",
                    )
                if verdict is _Reuse.PORT_CONFLICT:
                    raise PortConflictError(
                        f"port {existing.port} is held by an unrelated process.",
                        hint="Free the port or configure a different one.",
                    )
                if verdict is _Reuse.HUNG:
                    # Managed processes are alive but not serving; do not clear
                    # state or restart implicitly under a possibly-active session.
                    raise ProxyError(
                        "the managed proxy is running but not responding.",
                        hint="Run `agw proxy restart`.",
                    )
                clear_state(paths)  # DEAD/stale

            if is_port_open(HOST, config.port):
                raise PortConflictError(
                    f"port {config.port} on {HOST} is already in use by an unrelated process.",
                    hint="Free the port or set a different `port` in config.yaml.",
                )

            _require_authenticated_providers(paths, registry)
            _spawn_supervisor(
                paths,
                config,
                key,
                expected_fp,
                with_litellm=bool(registry.active_models()),
            )
            return _await_ready(paths, key, expected_models)
    except Timeout as exc:
        raise ProxyError(
            "timed out waiting for the proxy startup lock.",
            hint="Another `agw` process may be starting the proxy; retry shortly.",
        ) from exc


def stop(paths: Paths) -> bool:
    """Stop the managed proxy if this installation owns it. Returns True if stopped."""
    try:
        with _lock(paths).acquire(timeout=LOCK_TIMEOUT_SECONDS):
            state = read_state(paths)
            if state is None:
                return False
            terminate(state.router)
            if state.litellm is not None:
                terminate(state.litellm)
            terminate(state.supervisor)
            clear_state(paths)
            return True
    except Timeout as exc:
        raise ProxyError("timed out waiting for the proxy lock to stop.") from exc


def restart(paths: Paths, config: GatewayConfig, registry: ModelRegistry) -> ProxyState:
    """Explicit verified stop/start — the only path that applies a changed fingerprint."""
    stop(paths)
    return ensure_running(paths, config, registry)


@dataclass(frozen=True)
class ProxyStatus:
    running: bool
    healthy: bool
    state: ProxyState | None


def status(paths: Paths) -> ProxyStatus:
    """Report the managed proxy's liveness and health (no secrets)."""
    state = read_state(paths)
    if state is None:
        return ProxyStatus(running=False, healthy=False, state=None)
    running = (
        is_alive(state.supervisor)
        and is_alive(state.router)
        and (state.litellm is None or is_alive(state.litellm))
    )
    healthy = running and is_port_open(state.host, state.port)
    return ProxyStatus(running=running, healthy=healthy, state=state)
