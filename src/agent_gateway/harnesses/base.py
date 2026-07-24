"""The agent-harness interface.

A harness is the thing the developer actually runs (Claude Code in the MVP). Its
job is narrow: check prerequisites, resolve the real executable, build the child
environment that points the harness at the gateway, and launch it — replacing the
current process so stdio, signals, and the exit code pass through untouched.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NoReturn

from agent_gateway.config import GatewayConfig
from agent_gateway.model_registry import ModelRegistry
from agent_gateway.paths import Paths


class Harness(ABC):
    name: str

    @abstractmethod
    def resolve_executable(self, config: GatewayConfig) -> Path:
        """Return the absolute native executable path, or raise PrerequisiteError."""

    @abstractmethod
    def build_env(
        self,
        base_env: Mapping[str, str],
        config: GatewayConfig,
        registry: ModelRegistry,
        proxy_key: str,
        forwarded_args: Sequence[str],
    ) -> dict[str, str]:
        """Build the child environment (pure; no process side effects)."""

    @abstractmethod
    def launch(
        self,
        paths: Paths,
        config: GatewayConfig,
        registry: ModelRegistry,
        forwarded_args: Sequence[str],
    ) -> NoReturn:
        """Ensure the gateway is ready and exec the harness (never returns)."""
        raise NotImplementedError
