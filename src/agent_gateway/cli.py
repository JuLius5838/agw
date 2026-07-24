"""The ``agw`` command-line interface for setup, routing, and diagnostics."""

from __future__ import annotations

import functools
from collections.abc import Callable
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated

import typer

from agent_gateway import __version__
from agent_gateway.errors import GatewayError
from agent_gateway.providers import Provider

if TYPE_CHECKING:
    from agent_gateway.config import GatewayConfig
    from agent_gateway.model_registry import ModelRegistry
    from agent_gateway.paths import Paths

APP_NAME = "agw"

app = typer.Typer(
    name=APP_NAME,
    help=(
        "Agent Gateway — keep native Claude subscription routing while adding "
        "ChatGPT/Codex models to Claude Code."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def handle_errors[F: Callable[..., object]](func: F) -> F:
    """Translate a raised :class:`GatewayError` into a redacted message + exit code.

    ``functools.wraps`` preserves ``__wrapped__`` so Typer still introspects the
    original signature (its CLI arguments/options) rather than the wrapper's.
    """

    @functools.wraps(func)
    def wrapper(*args: object, **kwargs: object) -> object:
        try:
            return func(*args, **kwargs)
        except GatewayError as exc:
            _render_error(exc)
            raise typer.Exit(code=int(exc.exit_code)) from exc

    return wrapper  # type: ignore[return-value]


def _render_error(exc: GatewayError) -> None:
    typer.secho(f"error: {exc.message}", fg=typer.colors.RED, err=True)
    if exc.hint:
        typer.secho(f"hint: {exc.hint}", fg=typer.colors.YELLOW, err=True)


class ShellKind(StrEnum):
    """A supported interactive shell for bare-``claude`` integration."""

    zsh = "zsh"
    bash = "bash"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"{APP_NAME} {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    _version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the agent-gateway version and exit.",
        ),
    ] = False,
) -> None:
    """Agent Gateway CLI."""


# --------------------------------------------------------------------------- #
# setup
# --------------------------------------------------------------------------- #
@app.command()
@handle_errors
def setup(
    default_model: Annotated[
        str | None,
        typer.Option(help="Optional external startup model; omit for native Claude selection."),
    ] = None,
    provider_owner: Annotated[
        list[str] | None,
        typer.Option(
            "--provider-owner",
            help=(
                "MODEL=PROVIDER; enable that exact route and assign ownership if the "
                "name has multiple candidates. Repeatable."
            ),
        ),
    ] = None,
    agent_teams: Annotated[
        bool | None,
        typer.Option(
            "--agent-teams/--no-agent-teams",
            help="Enable Claude Code experimental agent teams (persisted in config).",
        ),
    ] = None,
    enable_shell: Annotated[
        ShellKind | None,
        typer.Option("--enable-shell", help="Select the shell for bare `claude` integration."),
    ] = None,
    no_shell: Annotated[
        bool,
        typer.Option("--no-shell", help="Do not modify any shell startup file."),
    ] = False,
) -> None:
    """Install local state, resolve models, and wire plain `claude` by default."""
    from agent_gateway.paths import get_paths
    from agent_gateway.setup import run_setup

    result = run_setup(
        get_paths(),
        default_model=default_model,
        provider_owner=provider_owner,
        agent_teams=agent_teams,
        enable_shell=enable_shell.value if enable_shell else None,
        no_shell=no_shell,
    )
    typer.secho("✓ agent-gateway is set up.", fg=typer.colors.GREEN)
    typer.echo(f"  native claude: {result.native_claude}")
    typer.echo(f"  default model: {result.default_model}")
    active = ", ".join(result.active_models) or "none (native Claude only)"
    typer.echo(f"  active external models: {active}")
    for note in result.notes:
        typer.echo(f"  - {note}")


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #
@app.command()
@handle_errors
def auth(
    provider: Annotated[Provider, typer.Argument(help="Which subscription to authenticate.")],
    model: Annotated[
        str | None,
        typer.Option(help="Model to use for the bounded authentication probe."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Reauthenticate even if current credentials are valid."),
    ] = False,
) -> None:
    """Authenticate a provider through its OAuth device flow (requires a TTY)."""
    from agent_gateway.auth import authenticate, get_adapter
    from agent_gateway.paths import get_paths

    adapter = get_adapter(provider)
    state = authenticate(get_paths(), adapter, model=model, force=force)
    typer.secho(f"✓ {adapter.display_name}: {state.detail}", fg=typer.colors.GREEN)


# --------------------------------------------------------------------------- #
# models
# --------------------------------------------------------------------------- #
models_app = typer.Typer(
    help="Inspect and verify configured models.",
    no_args_is_help=True,
)
app.add_typer(models_app, name="models")


@models_app.command("list")
@handle_errors
def models_list(
    all_: Annotated[
        bool,
        typer.Option("--all", help="Include inactive provider candidates."),
    ] = False,
    json_: Annotated[
        bool,
        typer.Option("--json", help="Emit a stable JSON document instead of a table."),
    ] = False,
) -> None:
    """List active public model names (and, with --all, inactive candidates)."""
    from agent_gateway.config import load_config
    from agent_gateway.model_registry import load_registry
    from agent_gateway.models import list_models, list_models_json, render_table
    from agent_gateway.paths import get_paths

    paths = get_paths()
    load_config(paths)  # ensures setup has run; raises ConfigError otherwise
    registry = load_registry(paths)
    if json_:
        typer.echo(list_models_json(registry, include_inactive=all_))
    else:
        typer.echo(render_table(list_models(registry, include_inactive=all_)))


@models_app.command("verify")
@handle_errors
def models_verify(
    model: Annotated[
        str | None,
        typer.Argument(help="Model to verify; defaults to every active model."),
    ] = None,
) -> None:
    """Verify a model's Claude Code compatibility through the Anthropic endpoint."""
    from agent_gateway import proxy
    from agent_gateway.errors import ModelUnavailableError
    from agent_gateway.models import CheckStatus, resolve_models_to_verify, verify_model
    from agent_gateway.secret_store import ensure_proxy_key

    paths, config, registry = _load_runtime()
    entries = resolve_models_to_verify(registry, model)
    state = proxy.ensure_running(paths, config, registry)
    key = ensure_proxy_key(paths)

    any_failed = False
    for entry in entries:
        report = verify_model(state, key, entry)
        marker = "✓" if report.ok else "✗"
        color = typer.colors.GREEN if report.ok else typer.colors.RED
        typer.secho(f"{marker} {report.model} ({report.provider})", fg=color)
        for check in report.checks:
            symbol = "✓" if check.status is CheckStatus.passed else "✗"
            typer.echo(f"    {symbol} {check.name}: {check.detail}")
        any_failed = any_failed or not report.ok

    if any_failed:
        raise ModelUnavailableError("one or more models failed Claude-compatibility verification.")


# --------------------------------------------------------------------------- #
# unified usage
# --------------------------------------------------------------------------- #
@app.command()
@handle_errors
def usage(
    json_: Annotated[
        bool,
        typer.Option("--json", help="Emit the stable, secret-free usage document."),
    ] = False,
) -> None:
    """Show Claude Code and ChatGPT/Codex subscription usage together."""
    import json

    from agent_gateway.paths import get_paths
    from agent_gateway.usage import build_usage_report, render_usage

    report = build_usage_report(get_paths())
    if json_:
        typer.echo(json.dumps(report, indent=2, sort_keys=True))
    else:
        typer.echo(render_usage(report))


@app.command("capture-claude-usage", hidden=True)
def capture_claude_usage() -> None:
    """Capture documented Claude usage fields from status-line stdin."""
    import sys

    from agent_gateway.paths import get_paths
    from agent_gateway.usage import capture_claude_status

    # A status-line collector must never disrupt Claude Code. Invalid/missing
    # payloads are normal before the first response and intentionally emit no text.
    try:
        capture_claude_status(get_paths(), sys.stdin.read())
    except Exception:  # noqa: BLE001
        return


@app.command("claude-extension-server", hidden=True)
def claude_extension_server() -> None:
    """Serve the local AGW Claude plugin over MCP stdio."""
    from agent_gateway.claude_extension import main

    main()


@app.command("claude-usage-guard", hidden=True)
def claude_usage_guard() -> None:
    """Block ``/agw-usage`` and provide a same-terminal fail-closed fallback."""
    import sys

    from agent_gateway.claude_extension import run_usage_guard
    from agent_gateway.paths import get_paths

    typer.echo(run_usage_guard(sys.stdin.read(), get_paths()))


# --------------------------------------------------------------------------- #
# proxy
# --------------------------------------------------------------------------- #
proxy_app = typer.Typer(
    help="Manage the loopback AGW router and optional private LiteLLM child.",
    no_args_is_help=True,
)
app.add_typer(proxy_app, name="proxy")


def _load_runtime() -> tuple[Paths, GatewayConfig, ModelRegistry]:
    """Load paths, config, and the model registry (raises ConfigError if unset)."""
    from agent_gateway.config import load_config
    from agent_gateway.model_registry import load_registry
    from agent_gateway.paths import get_paths

    paths = get_paths()
    return paths, load_config(paths), load_registry(paths)


@proxy_app.command("start")
@handle_errors
def proxy_start() -> None:
    """Start (or reuse) the managed proxy."""
    from agent_gateway import proxy

    paths, config, registry = _load_runtime()
    state = proxy.ensure_running(paths, config, registry)
    typer.secho(
        f"✓ gateway ready at {state.url}",
        fg=typer.colors.GREEN,
    )


@proxy_app.command("stop")
@handle_errors
def proxy_stop() -> None:
    """Stop the managed proxy, if this installation owns it."""
    from agent_gateway import proxy
    from agent_gateway.paths import get_paths

    if proxy.stop(get_paths()):
        typer.secho("✓ proxy stopped", fg=typer.colors.GREEN)
    else:
        typer.echo("no managed proxy was running")


@proxy_app.command("restart")
@handle_errors
def proxy_restart() -> None:
    """Verified stop/start; the only path that applies a changed config fingerprint."""
    from agent_gateway import proxy

    paths, config, registry = _load_runtime()
    state = proxy.restart(paths, config, registry)
    typer.secho(f"✓ proxy restarted at {state.url}", fg=typer.colors.GREEN)


@proxy_app.command("status")
@handle_errors
def proxy_status() -> None:
    """Report the managed proxy's process identity, address, and readiness."""
    from agent_gateway import proxy
    from agent_gateway.paths import get_paths

    result = proxy.status(get_paths())
    if not result.running or result.state is None:
        typer.echo("proxy: not running")
        return
    state = result.state
    health = "healthy" if result.healthy else "unhealthy"
    typer.echo(f"proxy: running ({health}) at {state.url}")
    typer.echo(f"  supervisor pid: {state.supervisor.pid}")
    typer.echo(f"  router pid:     {state.router.pid}")
    if state.litellm is not None:
        typer.echo(f"  litellm pid:    {state.litellm.pid} (litellm {state.litellm_version})")
    else:
        typer.echo("  litellm:        not started (no active external models)")
    typer.echo(f"  log:            {state.log_path}")


# --------------------------------------------------------------------------- #
# claude (verbatim passthrough launcher)
# --------------------------------------------------------------------------- #
@app.command(
    add_help_option=False,
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
        "help_option_names": [],
    },
)
@handle_errors
def claude(ctx: typer.Context) -> None:
    """Launch Claude Code through the managed gateway.

    All arguments after ``claude`` — including unknown options, repeated options,
    ``--``, and positionals — are forwarded verbatim to the native Claude Code
    executable. ``agw`` never reinterprets Claude Code flags.
    """
    import sys

    from agent_gateway.harnesses.claude import ClaudeHarness

    # Reconstruct the forwarded arguments from the raw argv. Click strips the first
    # ``--`` from ctx.args, so we slice everything after the `claude` token to
    # preserve arguments byte-for-byte (including ``--``).
    raw = sys.argv[1:]
    try:
        forwarded = raw[raw.index("claude") + 1 :]
    except ValueError:  # pragma: no cover - "claude" is always present here
        forwarded = list(ctx.args)

    paths, config, registry = _load_runtime()
    ClaudeHarness().launch(paths, config, registry, forwarded)


# --------------------------------------------------------------------------- #
# shell
# --------------------------------------------------------------------------- #
shell_app = typer.Typer(
    help="Manage the bare-`claude` shell integration.",
    no_args_is_help=True,
)
app.add_typer(shell_app, name="shell")


@shell_app.command("enable")
@handle_errors
def shell_enable(
    shell: Annotated[
        ShellKind | None,
        typer.Argument(help="Shell to configure; defaults to $SHELL when supported."),
    ] = None,
) -> None:
    """Wire bare `claude` to `agw claude` via a uniquely marked, reversible block."""
    from agent_gateway import shell as shell_mod
    from agent_gateway.config import load_config
    from agent_gateway.paths import get_paths

    paths = get_paths()
    result = shell_mod.enable(paths, load_config(paths), shell.value if shell else None)
    typer.secho(f"✓ shell integration {result.message}", fg=typer.colors.GREEN)
    typer.echo(f"  activate in the current shell: {result.activate_command}")


@shell_app.command("disable")
@handle_errors
def shell_disable(
    shell: Annotated[
        ShellKind | None,
        typer.Argument(help="Shell to unconfigure; defaults to $SHELL when supported."),
    ] = None,
) -> None:
    """Remove only the managed shell block and generated source file."""
    from agent_gateway import shell as shell_mod
    from agent_gateway.config import load_config
    from agent_gateway.paths import get_paths

    paths = get_paths()
    result = shell_mod.disable(paths, load_config(paths), shell.value if shell else None)
    typer.secho(f"✓ shell integration {result.message}", fg=typer.colors.GREEN)
    if result.changed:
        typer.echo(f"  remove from the current shell: {result.activate_command}")


@shell_app.command("status")
@handle_errors
def shell_status(
    shell: Annotated[
        ShellKind | None,
        typer.Argument(help="Shell to inspect; defaults to $SHELL when supported."),
    ] = None,
) -> None:
    """Report whether bare-`claude` integration is configured for the shell."""
    import os

    from agent_gateway import shell as shell_mod

    resolved = shell_mod.resolve_shell(shell.value if shell else None, os.environ)
    if shell_mod.is_enabled(resolved, os.environ):
        typer.echo(f"shell integration: enabled for {resolved}")
    else:
        typer.echo(f"shell integration: not enabled for {resolved}")


# --------------------------------------------------------------------------- #
# doctor / status / logs / uninstall
# --------------------------------------------------------------------------- #
@app.command()
@handle_errors
def doctor() -> None:
    """Report prerequisites, paths, permissions, proxy state, models, and auth."""
    from agent_gateway.doctor import Level, run_doctor
    from agent_gateway.errors import ConfigError
    from agent_gateway.paths import get_paths

    report = run_doctor(get_paths())
    symbols = {
        Level.ok: ("✓", typer.colors.GREEN),
        Level.warn: ("!", typer.colors.YELLOW),
        Level.fail: ("✗", typer.colors.RED),
    }
    for check in report.checks:
        symbol, color = symbols[check.level]
        typer.secho(f"{symbol} {check.name}: {check.detail}", fg=color)
    if report.failed:
        raise ConfigError("doctor found blocking problems.")


@app.command()
@handle_errors
def status() -> None:
    """Show the managed proxy identity and the public model-to-provider mapping."""
    from agent_gateway import proxy
    from agent_gateway.config import load_config
    from agent_gateway.model_registry import load_registry
    from agent_gateway.paths import get_paths

    paths = get_paths()
    load_config(paths)
    registry = load_registry(paths)
    result = proxy.status(paths)
    if result.state is None:
        typer.echo("proxy: not running")
    else:
        st = result.state
        health = "healthy" if result.healthy else ("running" if result.running else "stale")
        typer.echo(f"gateway: {health} at {st.url}")
        typer.echo(f"  supervisor pid {st.supervisor.pid}, router pid {st.router.pid}")
        if st.litellm is not None:
            typer.echo(f"  litellm pid {st.litellm.pid} (litellm {st.litellm_version})")
    typer.echo(f"startup model: {registry.default_model or 'native Claude selection'}")
    typer.echo("external models:")
    for entry in registry.active_models():
        typer.echo(f"  {entry.name} -> {entry.provider.value} ({entry.mode.value})")


@app.command()
@handle_errors
def logs(
    lines: Annotated[
        int,
        typer.Option("--lines", "-n", help="Number of trailing log lines to show."),
    ] = 100,
) -> None:
    """Show sanitized local operational logs (never prompt bodies or secrets)."""
    from agent_gateway.paths import get_paths, read_text
    from agent_gateway.redaction import redact

    log_file = get_paths().proxy_log
    if not log_file.is_file():
        typer.echo("(no logs yet)")
        return
    # Re-run every line through the redactor even though it was sanitized at write.
    tail = read_text(log_file).splitlines()[-lines:]
    for line in tail:
        typer.echo(redact(line))


@app.command()
@handle_errors
def uninstall(
    credentials: Annotated[
        bool,
        typer.Option("--credentials", help="Also delete stored OAuth credential directories."),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Non-interactive acknowledgement for --credentials."),
    ] = False,
) -> None:
    """Stop the managed proxy and remove generated runtime/shell files."""
    import sys

    from agent_gateway.paths import get_paths
    from agent_gateway.uninstall import run_uninstall

    acknowledged = yes
    # Interactive confirmation for credential deletion; non-TTY without --yes stays
    # False so run_uninstall refuses with guidance.
    if credentials and not yes and sys.stdin.isatty():
        acknowledged = typer.confirm(
            "Delete all stored AGW OAuth credentials, including legacy providers?",
            default=False,
        )

    result = run_uninstall(get_paths(), credentials=credentials, acknowledged=acknowledged)
    typer.secho("✓ uninstalled agent-gateway runtime.", fg=typer.colors.GREEN)
    if result.proxy_stopped:
        typer.echo("  stopped the managed proxy")
    for path in result.removed:
        typer.echo(f"  removed {path}")
    for note in result.notes:
        typer.echo(f"  - {note}")
