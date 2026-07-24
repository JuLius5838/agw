"""The Claude Code harness for the hybrid front router.

Claude Code keeps its saved subscription login: the child receives a base URL and
a local routing header, but never an Anthropic API/auth token from AGW. Native
model defaults remain untouched; only an explicitly configured external startup
default is injected. ``os.execve`` preserves stdio, signals, and exit status.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NoReturn

from agent_gateway import proxy
from agent_gateway.config import GatewayConfig, validate_native_claude_path
from agent_gateway.errors import PrerequisiteError
from agent_gateway.harnesses.base import Harness
from agent_gateway.model_registry import ModelRegistry
from agent_gateway.paths import Paths
from agent_gateway.secret_store import ensure_proxy_key

# Cloud routing flags that take precedence over ANTHROPIC_BASE_URL and would
# bypass the gateway; removed for the gateway child only.
_CLOUD_ROUTING_FLAGS = (
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)

_CUSTOM_MODEL_VARS = (
    "ANTHROPIC_CUSTOM_MODEL_OPTION",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION",
)

# Variables that locate external-provider credentials (and the internal proxy
# master key). The managed LiteLLM child needs these; the native Claude child
# must never inherit them, even if the surrounding shell exported one — including
# the retired Copilot variable left over from an earlier install.
_PROVIDER_SECRET_VARS = (
    "CHATGPT_TOKEN_DIR",
    "CHATGPT_AUTH_FILE",
    "GITHUB_COPILOT_TOKEN_DIR",
    "LITELLM_MASTER_KEY",
)


def _has_cli_option(args: Sequence[str], option: str) -> bool:
    for arg in args:
        if arg == "--":
            return False
        if arg == option or arg.startswith(f"{option}="):
            return True
    return False


def _has_explicit_model(args: Sequence[str]) -> bool:
    return _has_cli_option(args, "--model")


def _has_explicit_effort(args: Sequence[str]) -> bool:
    return _has_cli_option(args, "--effort")


def _launch_args(config: GatewayConfig, forwarded_args: Sequence[str]) -> list[str]:
    """Apply an overridable startup effort without creating an env hard override."""
    args = list(forwarded_args)
    if config.default_effort is not None and not _has_explicit_effort(args):
        return ["--effort", config.default_effort, *args]
    return args


def _custom_headers(existing: str | None, proxy_key: str) -> str:
    """Preserve custom headers while replacing any stale local AGW key."""
    kept = []
    for line in (existing or "").splitlines():
        name, separator, _value = line.partition(":")
        if separator and name.strip().lower() == "x-agw-key":
            continue
        if line.strip():
            kept.append(line)
    kept.append(f"X-AGW-Key: {proxy_key}")
    return "\n".join(kept)


class ClaudeHarness(Harness):
    name = "claude"

    def resolve_executable(self, config: GatewayConfig) -> Path:
        if not config.native_claude_path:
            raise PrerequisiteError(
                "the native Claude executable path is not configured.",
                hint="Run `agw setup`.",
            )
        return validate_native_claude_path(config.native_claude_path)

    def build_env(
        self,
        base_env: Mapping[str, str],
        config: GatewayConfig,
        registry: ModelRegistry,
        proxy_key: str,
        forwarded_args: Sequence[str],
    ) -> dict[str, str]:
        env = dict(base_env)
        env["ANTHROPIC_BASE_URL"] = f"http://{proxy.HOST}:{config.port}"
        env["ANTHROPIC_CUSTOM_HEADERS"] = _custom_headers(
            env.get("ANTHROPIC_CUSTOM_HEADERS"), proxy_key
        )
        env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] = "1"

        # Leaving these unset is what preserves Claude Code's saved claude.ai
        # subscription credential when ANTHROPIC_BASE_URL is changed.
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        env.pop("ANTHROPIC_API_KEY", None)
        if config.default_effort is not None:
            # Claude gives this variable precedence over --effort and /effort.
            # A configured AGW default is injected as a launch flag instead so
            # the user can still change effort during the session.
            env.pop("CLAUDE_CODE_EFFORT_LEVEL", None)

        # Claude Code 2.1.218 only performs multi-model gateway discovery when
        # an ANTHROPIC_AUTH_TOKEN/API_KEY gateway credential is present. AGW
        # intentionally cannot set one because it would replace the saved
        # claude.ai subscription for native requests. Use Claude's supported
        # single custom-model option as the safe picker fallback.
        active_external = registry.active_models()
        picker_entry = registry.default_entry() or (active_external[0] if active_external else None)
        if picker_entry is not None:
            env["ANTHROPIC_CUSTOM_MODEL_OPTION"] = picker_entry.name
            env["ANTHROPIC_CUSTOM_MODEL_OPTION_NAME"] = (
                picker_entry.display_name or picker_entry.name
            )
            env["ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION"] = (
                f"External via Agent Gateway ({picker_entry.provider.value})"
            )
        else:
            for variable in _CUSTOM_MODEL_VARS:
                env.pop(variable, None)

        explicit_model = _has_explicit_model(forwarded_args)
        if explicit_model:
            env.pop("ANTHROPIC_MODEL", None)
        elif registry.default_model is not None:
            env["ANTHROPIC_MODEL"] = registry.default_model

        # A global subagent override would defeat per-invocation model selection.
        env.pop("CLAUDE_CODE_SUBAGENT_MODEL", None)
        for flag in _CLOUD_ROUTING_FLAGS:
            env.pop(flag, None)

        if config.agent_teams_enabled:
            env["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] = "1"

        # Provider token-dir variables are never added here, and any inherited
        # from the surrounding shell are scrubbed so external-provider credential
        # paths and the internal proxy key cannot reach the native Claude child.
        for variable in _PROVIDER_SECRET_VARS:
            env.pop(variable, None)
        return env

    def launch(
        self,
        paths: Paths,
        config: GatewayConfig,
        registry: ModelRegistry,
        forwarded_args: Sequence[str],
    ) -> NoReturn:
        executable = self.resolve_executable(config)
        proxy_key = ensure_proxy_key(paths)
        proxy.ensure_running(paths, config, registry)
        launch_args = _launch_args(config, forwarded_args)
        env = self.build_env(os.environ, config, registry, proxy_key, launch_args)
        argv = [str(executable), *launch_args]
        # Replaces this process: stdio, signals, and exit status pass through, and
        # a `claude` shell function cannot recurse (we exec the resolved binary).
        os.execve(str(executable), argv, env)
