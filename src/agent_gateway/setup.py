"""``agw setup``: initialize per-user state and resolve the model registry.

Setup is non-interactive and preserves user-edited external model entries. It:
  * verifies the native Claude executable exists,
  * creates the runtime directories and the loopback proxy key,
  * installs the packaged default registry on first run (preserving local edits),
  * applies any ``--provider-owner`` and ``--default-model`` choices, validating
    the result, and
  * migrates registry entries for providers removed from the current release, and
  * enables plain ``claude`` shell integration by default when the shell is known.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from agent_gateway.claude_integration import install_claude_usage, uninstall_claude_usage
from agent_gateway.config import (
    GatewayConfig,
    discover_codex_cli,
    discover_native_claude,
    load_config,
    save_config,
)
from agent_gateway.errors import ConfigError, PrerequisiteError
from agent_gateway.model_registry import load_registry_text
from agent_gateway.paths import (
    Paths,
    atomic_write_text,
    ensure_dir,
    packaged_default_models_yaml,
    read_text,
)
from agent_gateway.providers import Provider
from agent_gateway.secret_store import ensure_proxy_key
from agent_gateway.shell import enable as shell_enable


@dataclass
class SetupResult:
    native_claude: str
    default_model: str
    active_models: list[str]
    shell_enabled: str | None = None
    notes: list[str] = field(default_factory=list)


def _same_directory(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except OSError:
        return left.resolve(strict=False) == right.resolve(strict=False)


def _parse_provider_owner(entries: Sequence[str]) -> dict[str, Provider]:
    owners: dict[str, Provider] = {}
    for entry in entries:
        name, sep, provider_str = entry.partition("=")
        if not sep or not name or not provider_str:
            raise ConfigError(
                f"invalid --provider-owner value: {entry!r}", hint="Use MODEL=PROVIDER."
            )
        try:
            owners[name] = Provider(provider_str)
        except ValueError as exc:
            raise ConfigError(
                f"unknown provider in --provider-owner {entry!r}: {provider_str}",
                hint="Provider must be `chatgpt` or `copilot`.",
            ) from exc
    return owners


def _apply_registry_choices(
    raw_yaml: str,
    *,
    default_model: str | None,
    provider_owners: Mapping[str, Provider],
) -> str:
    """Apply owner/default choices to raw registry YAML and return new YAML text."""
    data = yaml.safe_load(raw_yaml) or {}
    if not isinstance(data, dict):
        raise ConfigError("model registry must be a mapping.")
    models = data.get("models", [])
    if not isinstance(models, list):
        raise ConfigError("model registry `models` must be a list.")

    for name, provider in provider_owners.items():
        matches = [m for m in models if isinstance(m, dict) and m.get("name") == name]
        if not matches:
            raise ConfigError(f"--provider-owner names unknown model: {name}")
        if not any(m.get("provider") == provider.value for m in matches):
            raise ConfigError(
                f"model {name} has no candidate for provider {provider.value}.",
            )
        for candidate in matches:
            candidate["enabled"] = candidate.get("provider") == provider.value

    if default_model is not None:
        data["default_model"] = default_model

    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)


_REMOVED_REGISTRY_PROVIDERS = frozenset({"anthropic"})


def _migrate_removed_providers(raw_yaml: str) -> tuple[str, frozenset[str]]:
    """Remove entries for providers retired from the current release.

    Supported entries and their ordering are retained. A removed external default
    becomes ``null`` when no supported entry with the same public name remains, so
    Claude Code safely falls back to its native startup selection.
    """
    data = yaml.safe_load(raw_yaml) or {}
    if not isinstance(data, dict):
        raise ConfigError("model registry must be a mapping.")
    models = data.get("models", [])
    if not isinstance(models, list):
        raise ConfigError("model registry `models` must be a list.")

    removed_providers = frozenset(
        str(model.get("provider"))
        for model in models
        if isinstance(model, dict) and model.get("provider") in _REMOVED_REGISTRY_PROVIDERS
    )
    removed_names = {
        str(model.get("name"))
        for model in models
        if isinstance(model, dict) and model.get("provider") in _REMOVED_REGISTRY_PROVIDERS
    }
    if not removed_names:
        return raw_yaml, frozenset()

    kept = [
        model
        for model in models
        if not (isinstance(model, dict) and model.get("provider") in _REMOVED_REGISTRY_PROVIDERS)
    ]
    data["models"] = kept
    remaining_active_names = {
        str(model.get("name"))
        for model in kept
        if isinstance(model, dict) and model.get("name") and model.get("enabled") is True
    }
    if (
        data.get("default_model") in removed_names
        and data.get("default_model") not in remaining_active_names
    ):
        data["default_model"] = None
    return (
        yaml.safe_dump(data, sort_keys=False, default_flow_style=False),
        removed_providers,
    )


def _backup_registry_before_retired_provider_migration(
    paths: Paths,
    raw_yaml: str,
) -> tuple[Path, bool]:
    """Create one permission-safe, non-overwriting rollback copy of the registry."""
    backup = paths.models_file.with_name("models.pre-retired-providers.yaml")
    if backup.exists() or backup.is_symlink():
        return backup, False
    atomic_write_text(backup, raw_yaml, mode=0o600)
    return backup, True


def run_setup(
    paths: Paths,
    *,
    default_model: str | None = None,
    provider_owner: Sequence[str] | None = None,
    agent_teams: bool | None = None,
    enable_shell: str | None = None,
    no_shell: bool = False,
    env: Mapping[str, str] | None = None,
) -> SetupResult:
    native = discover_native_claude(dict(env) if env is not None else None)
    if native is None:
        raise PrerequisiteError(
            "could not find the native Claude executable on PATH.",
            hint="Install Claude Code (see https://code.claude.com) and re-run `agw setup`.",
        )

    ensure_dir(paths.config_dir)
    ensure_dir(paths.state_dir)
    ensure_proxy_key(paths)

    owners = _parse_provider_owner(provider_owner or [])
    notes: list[str] = []

    existed = paths.models_file.exists()
    base = read_text(paths.models_file) if existed else packaged_default_models_yaml()
    original_registry = base
    base, migrated_providers = _migrate_removed_providers(base)
    migration_backup: tuple[Path, bool] | None = None
    if existed and migrated_providers:
        migration_backup = _backup_registry_before_retired_provider_migration(
            paths,
            original_registry,
        )

    if existed and not (default_model or owners) and not migrated_providers:
        notes.append(f"kept existing model registry at {paths.models_file}")
        registry_text = base
    else:
        registry_text = _apply_registry_choices(
            base, default_model=default_model, provider_owners=owners
        )
        atomic_write_text(paths.models_file, registry_text, mode=0o600)
        if migrated_providers:
            notes.append(
                "removed retired model-provider entries: " + ", ".join(sorted(migrated_providers))
            )
            assert migration_backup is not None
            backup_path, created = migration_backup
            action = "saved" if created else "kept existing"
            notes.append(f"{action} pre-migration model registry at {backup_path}")

    registry = load_registry_text(registry_text)  # validates; raises ConfigError

    existing = load_config(paths) if paths.config_file.exists() else GatewayConfig()
    usage_install = install_claude_usage(env)
    if existing.claude_config_dir and not _same_directory(
        Path(existing.claude_config_dir),
        usage_install.claude_dir,
    ):
        notes.extend(uninstall_claude_usage(claude_dir=Path(existing.claude_config_dir)))
    config_updates: dict[str, object] = {
        "native_claude_path": str(native),
        "claude_config_dir": str(usage_install.claude_dir),
        "agent_teams_enabled": (
            agent_teams if agent_teams is not None else existing.agent_teams_enabled
        ),
    }
    codex_cli = discover_codex_cli(env)
    if codex_cli is not None:
        codex_path, codex_digest = codex_cli
        config_updates["codex_cli_path"] = str(codex_path)
        config_updates["codex_cli_sha256"] = codex_digest
        notes.append("pinned the current Codex CLI identity for private usage queries")
    config = existing.model_copy(update=config_updates)
    save_config(paths, config)

    result = SetupResult(
        native_claude=str(native),
        default_model=registry.default_model or "native Claude selection",
        active_models=[m.name for m in registry.active_models()],
        notes=notes,
    )

    result.notes.extend(usage_install.notes)

    if not no_shell:
        try:
            shell_result = shell_enable(paths, config, enable_shell, env)
        except ConfigError as exc:
            if enable_shell is not None:
                raise
            result.notes.append(
                f"plain `claude` integration not enabled automatically: {exc.message}"
            )
        else:
            result.shell_enabled = shell_result.shell
            result.notes.append(shell_result.message)
            result.notes.append(f"activate now with: {shell_result.activate_command}")
    else:
        result.notes.append(
            "shell integration skipped; use `agw claude` or run `agw shell enable`."
        )

    return result
