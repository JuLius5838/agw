"""Unit tests for the supervisor's sanitized log writer.

End-to-end supervisor behavior (spawning LiteLLM, writing state, draining, signal
handling) is exercised by the proxy-lifecycle contract test, which runs the
supervisor as a real detached subprocess.
"""

from __future__ import annotations

import os
import stat

import pytest

from agent_gateway.supervisor import _Log

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
