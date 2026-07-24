"""Reversible bare-``claude`` shell integration.

``enable`` writes a generated source file and adds a uniquely marked, guarded
block to the shell startup file that sources it. It is idempotent, preserves all
other startup-file content byte-for-byte, and refuses to touch a file that has
malformed/duplicate markers or a pre-existing ``claude`` alias/function it did not
create. ``disable`` removes only that block and the generated file.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from agent_gateway.config import GatewayConfig, ShellName, save_config
from agent_gateway.errors import ConfigError
from agent_gateway.paths import (
    Paths,
    atomic_write_text,
    ensure_dir,
    packaged_shell_snippet,
    read_text,
)

START_MARKER = "# >>> agent-gateway >>>"
END_MARKER = "# <<< agent-gateway <<<"

# A pre-existing `claude` alias/function we did not create (outside our block).
_FOREIGN_CLAUDE = re.compile(
    r"^\s*(?:alias\s+claude=|claude\s*\(\s*\)|function\s+claude\b)",
    re.MULTILINE,
)


@dataclass(frozen=True)
class ShellResult:
    shell: ShellName
    changed: bool
    startup_file: Path
    activate_command: str
    message: str


def resolve_shell(shell_arg: str | None, env: Mapping[str, str]) -> ShellName:
    if shell_arg in ("zsh", "bash"):
        return shell_arg  # type: ignore[return-value]
    if shell_arg:
        raise ConfigError(f"unsupported shell: {shell_arg}", hint="Use `zsh` or `bash`.")
    name = Path(env.get("SHELL", "")).name
    if name in ("zsh", "bash"):
        return name  # type: ignore[return-value]
    raise ConfigError(
        "could not determine the shell from $SHELL.",
        hint="Pass the shell explicitly: `agw shell enable zsh|bash`.",
    )


def startup_file(shell: ShellName, env: Mapping[str, str]) -> Path:
    home = Path(env.get("HOME") or os.path.expanduser("~"))
    if shell == "zsh":
        zdotdir = env.get("ZDOTDIR")
        return (Path(zdotdir) if zdotdir else home) / ".zshrc"
    return home / ".bashrc"


def _block(source_file: Path) -> str:
    return f'{START_MARKER}\n[ -r "{source_file}" ] && . "{source_file}"\n{END_MARKER}\n'


def _locate_block(content: str, startup: Path) -> tuple[list[str], tuple[int, int] | None]:
    """Return (lines, span) where span is the inclusive marker line range, or None.

    Raises :class:`ConfigError` on malformed or duplicated markers.
    """
    lines = content.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.strip() == START_MARKER]
    ends = [i for i, line in enumerate(lines) if line.strip() == END_MARKER]
    if not starts and not ends:
        return lines, None
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        raise ConfigError(
            f"malformed or duplicated agent-gateway markers in {startup}.",
            hint="Remove the agent-gateway block manually, then re-run.",
        )
    return lines, (starts[0], ends[0])


def _write_preserving_mode(path: Path, content: str) -> None:
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    atomic_write_text(path, content, mode=mode)


def _persist_shell_choice(paths: Paths, config: GatewayConfig, shell: ShellName | None) -> None:
    save_config(paths, config.model_copy(update={"shell_integration": shell}))


def enable(
    paths: Paths,
    config: GatewayConfig,
    shell_arg: str | None = None,
    env: Mapping[str, str] | None = None,
) -> ShellResult:
    env = os.environ if env is None else env
    shell = resolve_shell(shell_arg, env)
    startup = startup_file(shell, env)
    source_file = paths.shell_source_file(shell)

    ensure_dir(paths.shell_dir)
    atomic_write_text(source_file, packaged_shell_snippet(shell), mode=0o644)
    activate = f'source "{source_file}"'

    content = read_text(startup) if startup.exists() else ""
    _, span = _locate_block(content, startup)

    if span is not None:
        _persist_shell_choice(paths, config, shell)
        return ShellResult(
            shell=shell,
            changed=False,
            startup_file=startup,
            activate_command=activate,
            message=f"already enabled in {startup}",
        )

    if _FOREIGN_CLAUDE.search(content):
        raise ConfigError(
            f"`claude` is already defined in {startup}; refusing to edit it.",
            hint="Remove or rename that definition, then re-run `agw shell enable`.",
        )

    new_content = content
    if new_content and not new_content.endswith("\n"):
        new_content += "\n"
    new_content += "\n" + _block(source_file)
    _write_preserving_mode(startup, new_content)
    _persist_shell_choice(paths, config, shell)
    return ShellResult(
        shell=shell,
        changed=True,
        startup_file=startup,
        activate_command=activate,
        message=f"enabled in {startup} (applies to new shells)",
    )


def disable(
    paths: Paths,
    config: GatewayConfig,
    shell_arg: str | None = None,
    env: Mapping[str, str] | None = None,
) -> ShellResult:
    env = os.environ if env is None else env
    shell = resolve_shell(shell_arg, env)
    startup = startup_file(shell, env)
    source_file = paths.shell_source_file(shell)

    removed = False
    if startup.exists():
        lines, span = _locate_block(read_text(startup), startup)
        if span is not None:
            start, end = span
            # Also drop a single blank separator line immediately before the block.
            if start > 0 and lines[start - 1].strip() == "":
                start -= 1
            kept = lines[:start] + lines[end + 1 :]
            _write_preserving_mode(startup, "".join(kept))
            removed = True

    if source_file.exists():
        source_file.unlink()
    _persist_shell_choice(paths, config, None)

    unset = "unfunction claude" if shell == "zsh" else "unset -f claude"
    return ShellResult(
        shell=shell,
        changed=removed,
        startup_file=startup,
        activate_command=unset,
        message=(f"disabled in {startup}" if removed else "was not enabled"),
    )


def is_enabled(shell: ShellName, env: Mapping[str, str]) -> bool:
    startup = startup_file(shell, env)
    if not startup.exists():
        return False
    _, span = _locate_block(read_text(startup), startup)
    return span is not None
