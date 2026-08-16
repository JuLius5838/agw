"""Unit tests for the supervisor's sanitized log writer.

End-to-end supervisor behavior (spawning LiteLLM, writing state, draining, signal
handling) is exercised by the proxy-lifecycle contract test, which runs the
supervisor as a real detached subprocess.
"""

from __future__ import annotations

import os
import stat

import pytest

from agent_gateway import supervisor as supervisor_mod
from agent_gateway.supervisor import _Log, _parse_args, _router_env

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX permission semantics")


def test_log_writes_events_and_filters_litellm_output(tmp_path):
    log_file = tmp_path / "logs" / "proxy.log"
    log = _Log(log_file)
    log.event("supervisor starting")
    log.litellm("INFO: Application startup complete.\n")  # kept
    log.litellm("POST /v1/messages body={'secret prompt here'}\n")  # dropped
    log.litellm("ERROR: Authorization: Bearer sk-secret-xyz123 denied\n")  # kept, redacted
    log.close()

    content = log_file.read_text()
    assert "supervisor starting" in content
    assert "Application startup complete" in content
    assert "secret prompt here" not in content
    assert "sk-secret-xyz123" not in content


@posix_only
def test_log_file_is_0600(tmp_path):
    log_file = tmp_path / "logs" / "proxy.log"
    log = _Log(log_file)
    log.event("hello")
    log.close()
    assert stat.S_IMODE(log_file.stat().st_mode) == 0o600


# --------------------------------------------------------------------------- #
# Probe-directory propagation and the router's secret boundary
# --------------------------------------------------------------------------- #
BASE_ARGS = [
    "--host",
    "127.0.0.1",
    "--port",
    "4000",
    "--models-file",
    "/tmp/models.yaml",
    "--log-file",
    "/tmp/proxy.log",
    "--state-file",
    "/tmp/proxy.json",
    "--litellm-version",
    "1.93.0",
    "--config-fingerprint",
    "cfg",
    "--runtime-fingerprint",
    "rt",
]


def test_probe_dir_is_required(capsys):
    with pytest.raises(SystemExit):
        _parse_args(BASE_ARGS)


def test_probe_dir_is_parsed(tmp_path):
    args = _parse_args([*BASE_ARGS, "--probe-dir", str(tmp_path / "probes")])
    assert args.probe_dir == str(tmp_path / "probes")


def test_router_is_spawned_with_exactly_one_probe_dir(monkeypatch, tmp_path):
    spawned: list[list[str]] = []

    class _FakeProc:
        pid = 4321
        stdout = None

        def poll(self):
            return 0  # already exited, so no signal is sent during teardown

        def terminate(self):  # pragma: no cover - defensive
            raise AssertionError("a exited child must not be signalled")

        def wait(self, timeout=None):
            return 0

    def fake_spawn(command, env):
        spawned.append(list(command))
        return _FakeProc()

    monkeypatch.setattr(supervisor_mod, "_spawn", fake_spawn)
    monkeypatch.setattr(supervisor_mod, "identity_of", lambda pid: None)

    probe_dir = tmp_path / "probes"
    log_file = tmp_path / "proxy.log"
    args = [
        *BASE_ARGS[:6],
        "--log-file",
        str(log_file),
        "--state-file",
        str(tmp_path / "proxy.json"),
        "--litellm-version",
        "1.93.0",
        "--config-fingerprint",
        "cfg",
        "--runtime-fingerprint",
        "rt",
        "--probe-dir",
        str(probe_dir),
    ]
    # identity_of returns None, so main() reports the failed-start path and exits
    # after spawning; the spawn arguments are what this test inspects.
    supervisor_mod.main(args)

    router_command = spawned[-1]
    assert router_command.count("--probe-dir") == 1
    assert router_command[router_command.index("--probe-dir") + 1] == str(probe_dir)


def test_router_env_keeps_router_keys_and_drops_provider_secrets(monkeypatch):
    for name, value in {
        "AGW_LOCAL_KEY": "local",
        "AGW_LITELLM_KEY": "internal",
        "CHATGPT_TOKEN_DIR": "/creds/chatgpt",
        "CHATGPT_AUTH_FILE": "/creds/chatgpt/auth.json",
        "GITHUB_COPILOT_TOKEN_DIR": "/creds/copilot",
        "LITELLM_MASTER_KEY": "master",
    }.items():
        monkeypatch.setenv(name, value)

    env = _router_env()
    assert env["AGW_LOCAL_KEY"] == "local"
    assert env["AGW_LITELLM_KEY"] == "internal"
    for dropped in (
        "CHATGPT_TOKEN_DIR",
        "CHATGPT_AUTH_FILE",
        "GITHUB_COPILOT_TOKEN_DIR",
        "LITELLM_MASTER_KEY",
    ):
        assert dropped not in env
