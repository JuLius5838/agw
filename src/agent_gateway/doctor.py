"""``agw doctor``: offline checks plus a bounded managed-proxy probe.

Doctor never displays secret content. It separates cheap offline checks
(prerequisites, paths, permissions, config, models, provider auth-state,
Claude-allowlist conflicts) from an online managed-proxy probe. Each check has a
level: OK, WARN, or FAIL. ``run_doctor`` returns them so the CLI can render and
choose an exit code.
"""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from agent_gateway import proxy
from agent_gateway.config import GatewayConfig, load_config, read_claude_settings
from agent_gateway.errors import GatewayError
from agent_gateway.model_registry import ModelRegistry, load_registry
from agent_gateway.paths import Paths
from agent_gateway.proxy import litellm_version

_IS_POSIX = os.name == "posix"


class Level(StrEnum):
    ok = "ok"
    warn = "warn"
    fail = "fail"


@dataclass(frozen=True)
class Check:
    level: Level
    name: str
    detail: str


def _prereqs() -> list[Check]:
    checks: list[Check] = []
    uv = shutil.which("uv")
    checks.append(
        Check(Level.ok if uv else Level.warn, "uv", uv or "not found on PATH (needed for install)")
    )
    checks.append(Check(Level.ok, "litellm", f"version {litellm_version()}"))
    return checks


def _perm_check(path: Path, expected: int, label: str) -> Check:
    if not path.exists():
        return Check(Level.warn, label, f"missing: {path}")
    if not _IS_POSIX:
        return Check(Level.ok, label, f"{path} (permissions not checked on this platform)")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode == expected:
        return Check(Level.ok, label, f"{oct(mode)} {path}")
    return Check(Level.fail, label, f"{oct(mode)} (expected {oct(expected)}) {path}")


def _paths_and_perms(paths: Paths) -> list[Check]:
    checks: list[Check] = []
    if paths.credentials_dir.exists():
        checks.append(_perm_check(paths.credentials_dir, 0o700, "credentials dir perms"))
    if paths.proxy_key_file.exists():
        checks.append(_perm_check(paths.proxy_key_file, 0o600, "proxy-key perms"))
    for provider in ("chatgpt", "copilot"):
        provider_dir = paths.provider_credentials_dir(provider)
        if provider_dir.exists():
            checks.append(_perm_check(provider_dir, 0o700, f"{provider} creds dir perms"))
    return checks


def _native_claude(config: GatewayConfig) -> Check:
    from agent_gateway.config import validate_native_claude_path

    if not config.native_claude_path:
        return Check(Level.fail, "native claude", "not configured (run `agw setup`)")
    try:
        resolved = validate_native_claude_path(config.native_claude_path)
        return Check(Level.ok, "native claude", str(resolved))
    except GatewayError as exc:
        return Check(Level.fail, "native claude", exc.message)


def _conflicting_env(env: Mapping[str, str]) -> list[Check]:
    checks: list[Check] = []
    conflicts = {
        "CLAUDE_CODE_SUBAGENT_MODEL": "overrides per-invocation subagent model",
        "CLAUDE_CODE_USE_BEDROCK": "cloud routing flag overrides gateway auth",
        "CLAUDE_CODE_USE_VERTEX": "cloud routing flag overrides gateway auth",
        "CLAUDE_CODE_USE_FOUNDRY": "cloud routing flag overrides gateway auth",
        "ANTHROPIC_AUTH_TOKEN": "removed at launch so Claude Code keeps its saved subscription",
        "ANTHROPIC_API_KEY": "removed at launch so Claude Code keeps its saved subscription",
    }
    for var, why in conflicts.items():
        if env.get(var):
            checks.append(
                Check(
                    Level.warn, f"env {var}", f"set in your environment; {why} (removed at launch)"
                )
            )
    return checks


def _model_allowlist(registry: ModelRegistry, env: Mapping[str, str]) -> list[Check]:
    """Warn when an inspectable Claude availableModels policy excludes a gateway name."""
    checks: list[Check] = []
    for settings_path, settings in read_claude_settings(dict(env)):
        allow = settings.get("availableModels")
        if isinstance(allow, list) and allow:
            allowed = {str(x) for x in allow}
            excluded = {
                entry.name
                for entry in registry.active_models()
                if entry.name not in allowed and registry.picker_id(entry) not in allowed
            }
            if excluded:
                checks.append(
                    Check(
                        Level.warn,
                        "claude availableModels",
                        f"{settings_path} excludes: {', '.join(sorted(excluded))}",
                    )
                )
    return checks


def _providers(paths: Paths, registry: ModelRegistry, *, online: bool) -> list[Check]:
    from agent_gateway.auth import get_adapter
    from agent_gateway.providers.base import AuthStatus

    checks: list[Check] = []
    providers = {m.provider for m in registry.active_models()}
    for provider in sorted(providers, key=lambda p: p.value):
        adapter = get_adapter(provider)
        state = adapter.auth_state(paths)
        if state.status is not AuthStatus.authenticated:
            checks.append(
                Check(
                    Level.fail,
                    f"auth: {adapter.display_name}",
                    f"{state.detail} — run `{adapter.remediation()}`",
                )
            )
            continue
        # Credential present. When online, confirm it is actually usable (e.g. an
        # active Copilot subscription) so a lapsed plan surfaces here as a warning
        # rather than an opaque routing error at request time.
        if online:
            entitlement = adapter.entitlement(paths)
            if not entitlement.ok:
                checks.append(
                    Check(
                        Level.warn,
                        f"auth: {adapter.display_name}",
                        f"credential present, but {entitlement.detail}",
                    )
                )
                continue
        checks.append(Check(Level.ok, f"auth: {adapter.display_name}", state.detail))
    return checks


def _proxy_probe(paths: Paths) -> list[Check]:
    result = proxy.status(paths)
    if result.state is None:
        return [Check(Level.ok, "proxy", "not running (started on demand)")]
    state = result.state
    if result.healthy:
        return [Check(Level.ok, "proxy", f"running and listening at {state.url}")]
    if result.running:
        return [Check(Level.warn, "proxy", f"processes alive but not listening at {state.url}")]
    return [
        Check(
            Level.warn, "proxy", "stale state file (no live process); will be cleaned on next start"
        )
    ]


@dataclass
class DoctorReport:
    checks: list[Check]

    @property
    def failed(self) -> bool:
        return any(c.level is Level.fail for c in self.checks)


def run_doctor(
    paths: Paths, *, online: bool = True, env: Mapping[str, str] | None = None
) -> DoctorReport:
    environ = os.environ if env is None else env
    checks: list[Check] = []
    checks += _prereqs()
    checks += _conflicting_env(environ)

    try:
        config = load_config(paths)
    except GatewayError as exc:
        checks.append(Check(Level.fail, "config", exc.message))
        return DoctorReport(checks)

    checks.append(_native_claude(config))
    checks += _paths_and_perms(paths)

    try:
        registry = load_registry(paths)
        checks.append(
            Check(
                Level.ok,
                "models",
                f"native Claude + {len(registry.active_models())} external; "
                f"startup {registry.default_model or 'native Claude selection'}",
            )
        )
        checks += _providers(paths, registry, online=online)
        checks += _model_allowlist(registry, environ)
    except GatewayError as exc:
        checks.append(Check(Level.fail, "models", exc.message))
        return DoctorReport(checks)

    if online:
        checks += _proxy_probe(paths)
    return DoctorReport(checks)
