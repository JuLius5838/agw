"""Typed error hierarchy and stable process exit codes.

Every user-facing failure raised by the runtime is a :class:`GatewayError` (or a
subclass) carrying a stable :class:`ExitCode`. The CLI translates these into a
redacted stderr message plus the corresponding process exit status, so callers
and CI can branch on failure category without scraping text.

Exit code ``2`` is intentionally skipped: Typer/Click reserve it for usage
errors (unknown command, bad option), and we do not want to shadow that meaning.
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    """Stable process exit codes, grouped by failure category."""

    OK = 0
    GENERAL = 1
    # 2 is reserved by Typer/Click for usage errors.
    CONFIG = 3
    AUTH = 4
    MODEL_UNAVAILABLE = 5
    PORT_CONFLICT = 6
    PREREQUISITE = 7
    PROXY = 8
    NOT_IMPLEMENTED = 9
    INTERNAL = 10


class GatewayError(Exception):
    """Base class for all expected, user-facing gateway failures.

    ``message`` is shown to the user; ``hint`` (optional) is a follow-up line
    suggesting a remedy such as the exact command to run next. Neither field may
    contain secrets — callers are responsible for redacting before constructing
    the error.
    """

    exit_code: ExitCode = ExitCode.GENERAL

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class ConfigError(GatewayError):
    """Invalid, missing, or conflicting configuration or model registry."""

    exit_code = ExitCode.CONFIG


class AuthError(GatewayError):
    """A provider is not authenticated, or its credentials are expired/revoked."""

    exit_code = ExitCode.AUTH


class ModelUnavailableError(GatewayError):
    """A requested model is missing, unauthorized, removed, or tool-incompatible.

    The gateway never falls back to a different model; it surfaces the requested
    public name and provider so the failure is transparent.
    """

    exit_code = ExitCode.MODEL_UNAVAILABLE


class PortConflictError(GatewayError):
    """The configured proxy port is held by an unrelated (non-gateway) process."""

    exit_code = ExitCode.PORT_CONFLICT


class PrerequisiteError(GatewayError):
    """A required prerequisite (native Claude, uv, LiteLLM, TTY, ...) is missing."""

    exit_code = ExitCode.PREREQUISITE


class ProxyError(GatewayError):
    """The managed proxy failed to start, become ready, or is in a bad state."""

    exit_code = ExitCode.PROXY


class NotImplementedYetError(GatewayError):
    """A scaffolded command whose behavior lands in a later implementation task."""

    exit_code = ExitCode.NOT_IMPLEMENTED

    def __init__(self, command: str) -> None:
        super().__init__(
            f"`agw {command}` is not implemented yet.",
            hint="This command is scaffolded; its behavior arrives in a later task.",
        )


class InternalError(GatewayError):
    """An unexpected internal invariant was violated. Likely a bug in the gateway."""

    exit_code = ExitCode.INTERNAL
