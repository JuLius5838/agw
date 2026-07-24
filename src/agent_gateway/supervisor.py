"""Supervisor for the public AGW router and optional private LiteLLM child.

Runs detached in its own session. It always launches the public router and starts
the pinned LiteLLM on a separate loopback port only when external models are
active. It records every process identity and drains child output through the
redacting log filter. If either child exits, all remaining children are stopped.

The proxy key and provider token directories are provided through this process's
environment by the launcher; they are never passed on the command line or logged.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import FrameType

from agent_gateway.observability import format_event, sanitize_litellm_line
from agent_gateway.paths import ensure_dir
from agent_gateway.process import current_identity, identity_of
from agent_gateway.proxy import ProxyState, write_state


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="agent_gateway.supervisor")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--models-file", required=True)
    parser.add_argument("--litellm")
    parser.add_argument("--config")
    parser.add_argument("--litellm-host")
    parser.add_argument("--litellm-port", type=int)
    parser.add_argument("--log-file", required=True)
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--litellm-version", required=True)
    parser.add_argument("--config-fingerprint", required=True)
    parser.add_argument("--runtime-fingerprint", required=True)
    return parser.parse_args(argv)


class _Log:
    """Append-only 0600 log writer for sanitized supervisor output."""

    def __init__(self, path: Path) -> None:
        ensure_dir(path.parent)
        # Open with restrictive permissions from the start.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self._handle = os.fdopen(fd, "a", encoding="utf-8")

    def event(self, message: str) -> None:
        self._handle.write(format_event(message) + "\n")
        self._handle.flush()

    def child(self, line: str) -> None:
        sanitized = sanitize_litellm_line(line)
        if sanitized is not None:
            self._handle.write(sanitized + "\n")
            self._handle.flush()

    def litellm(self, line: str) -> None:
        """Compatibility name for callers that classify all child output as LiteLLM."""
        self.child(line)

    def close(self) -> None:
        self._handle.close()


def _litellm_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("AGW_LOCAL_KEY", None)
    env.pop("AGW_LITELLM_KEY", None)
    return env


def _router_env() -> dict[str, str]:
    env = os.environ.copy()
    for name in (
        "CHATGPT_TOKEN_DIR",
        "CHATGPT_AUTH_FILE",
        # Scrub the retired provider's legacy variable during upgrades too.
        "GITHUB_COPILOT_TOKEN_DIR",
        "LITELLM_MASTER_KEY",
    ):
        env.pop(name, None)
    return env


def _spawn(command: list[str], env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )


def _drain(proc: subprocess.Popen[str], log: _Log) -> None:
    assert proc.stdout is not None
    for line in proc.stdout:
        log.child(line)


def _stop_child(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5.0)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    log = _Log(Path(args.log_file))
    log.event("supervisor starting")
    children: list[tuple[str, subprocess.Popen[str]]] = []

    has_litellm = all(
        value is not None
        for value in (args.litellm, args.config, args.litellm_host, args.litellm_port)
    )
    any_litellm = any(
        value is not None
        for value in (args.litellm, args.config, args.litellm_host, args.litellm_port)
    )
    if any_litellm and not has_litellm:
        log.event("incomplete LiteLLM child configuration")
        log.close()
        return 2

    litellm_proc: subprocess.Popen[str] | None = None
    if has_litellm:
        litellm_proc = _spawn(
            [
                str(args.litellm),
                "--config",
                str(args.config),
                "--host",
                str(args.litellm_host),
                "--port",
                str(args.litellm_port),
            ],
            _litellm_env(),
        )
        children.append(("litellm", litellm_proc))

    router_command = [
        sys.executable,
        "-m",
        "agent_gateway.router",
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--models-file",
        args.models_file,
    ]
    if has_litellm:
        router_command.extend(["--litellm-url", f"http://{args.litellm_host}:{args.litellm_port}"])
    router_proc = _spawn(router_command, _router_env())
    children.append(("router", router_proc))

    router_identity = identity_of(router_proc.pid)
    litellm_identity = identity_of(litellm_proc.pid) if litellm_proc is not None else None
    if router_identity is None or (litellm_proc is not None and litellm_identity is None):
        log.event("a managed child failed to start")
        for _name, child in children:
            _stop_child(child)
        log.close()
        return 1

    state = ProxyState(
        supervisor=current_identity(),
        router=router_identity,
        litellm=litellm_identity,
        host=args.host,
        port=args.port,
        litellm_host=str(args.litellm_host) if has_litellm else None,
        litellm_port=int(args.litellm_port) if has_litellm else None,
        litellm_version=args.litellm_version,
        config_fingerprint=args.config_fingerprint,
        runtime_fingerprint=args.runtime_fingerprint,
        log_path=args.log_file,
    )
    write_state(Path(args.state_file), state)
    log.event(f"router started pid={router_proc.pid} on {args.host}:{args.port}")
    if litellm_proc is not None:
        log.event(
            f"litellm started pid={litellm_proc.pid} on {args.litellm_host}:{args.litellm_port}"
        )

    stopping = threading.Event()

    def _handle_term(_signum: int, _frame: FrameType | None) -> None:
        log.event("received termination signal; stopping children")
        stopping.set()

    signal.signal(signal.SIGTERM, _handle_term)
    signal.signal(signal.SIGINT, _handle_term)

    drainers = [
        threading.Thread(target=_drain, args=(child, log), daemon=True) for _name, child in children
    ]
    for drainer in drainers:
        drainer.start()

    exit_code = 0
    while not stopping.is_set():
        exited = next(
            ((name, child.poll()) for name, child in children if child.poll() is not None),
            None,
        )
        if exited is not None:
            name, code = exited
            log.event(f"{name} exited code={code}")
            exit_code = int(code or 1)
            break
        time.sleep(0.1)

    for _name, child in reversed(children):
        _stop_child(child)
    for drainer in drainers:
        drainer.join(timeout=2.0)

    _remove_own_state(Path(args.state_file))
    log.close()
    return exit_code


def _remove_own_state(state_file: Path) -> None:
    import json

    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        if int(data.get("supervisor", {}).get("pid", -1)) == os.getpid():
            state_file.unlink()
    except (OSError, ValueError, TypeError):
        pass


if __name__ == "__main__":
    sys.exit(main())
