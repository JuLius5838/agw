"""Contract test: boot the real (pinned) LiteLLM proxy offline and check /v1/models.

We prove the core contract Claude Code depends on — that a public model name is
exposed *unprefixed* through the proxy — by booting the actual pinned LiteLLM
against our rendered config, with a fake (non-expired) ``auth.json`` in a scoped
``CHATGPT_TOKEN_DIR`` so no device flow and no network call is triggered.

Marked ``contract``: hermetic (no network) and CI-safe, but slower than a unit
test because it starts the proxy process once per module.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from agent_gateway.litellm_config import render_litellm_config
from agent_gateway.model_registry import load_registry_text

pytestmark = [pytest.mark.contract, pytest.mark.live]

# Two chatgpt-backed public names → proves the exact-set contract, no-prefix
# exposure, and that both share one token directory.
REGISTRY_TEXT = textwrap.dedent(
    """
    default_model: alpha-codex
    models:
      - name: alpha-codex
        provider: chatgpt
        upstream_model: chatgpt/gpt-5.3-codex
        mode: responses
        enabled: true
      - name: beta-codex
        provider: chatgpt
        upstream_model: chatgpt/gpt-5.1-codex
        mode: responses
        enabled: true
      - name: hidden-candidate
        provider: chatgpt
        upstream_model: chatgpt/gpt-4.1
        mode: chat
        enabled: false
    """
)
EXPECTED_ACTIVE = {"alpha-codex", "beta-codex"}
MASTER_KEY = "sk-agw-contract-test-key"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _litellm_executable() -> str:
    candidate = Path(sys.executable).parent / "litellm"
    return str(candidate) if candidate.exists() else "litellm"


@pytest.fixture(scope="module")
def proxy(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, str]]:
    tmp = tmp_path_factory.mktemp("agw-contract")
    token_dir = tmp / "chatgpt"
    token_dir.mkdir()
    (token_dir / "auth.json").write_text(
        json.dumps(
            {
                "access_token": "fake-access-token-not-real",
                "refresh_token": "fake-refresh-token",
                "id_token": "fake-id-token",
                "expires_at": 9999999999,  # far future: never treated as expired
                "account_id": "acct_fake",
            }
        )
    )
    config = tmp / "litellm.yaml"
    config.write_text(render_litellm_config(load_registry_text(REGISTRY_TEXT)))

    port = _free_port()
    env = {
        **os.environ,
        "LITELLM_MASTER_KEY": MASTER_KEY,
        "CHATGPT_TOKEN_DIR": str(token_dir),
        "CHATGPT_AUTH_FILE": "auth.json",
    }
    proc = subprocess.Popen(
        [
            _litellm_executable(),
            "--config",
            str(config),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    captured: list[str] = []

    def _drain() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            captured.append(line)

    threading.Thread(target=_drain, daemon=True).start()

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 60
    ready = False
    while time.time() < deadline:
        if proc.poll() is not None:
            pytest.fail(
                f"litellm exited early (code {proc.returncode}):\n{''.join(captured)[-2000:]}"
            )
        try:
            resp = httpx.get(
                f"{base}/v1/models",
                headers={"Authorization": f"Bearer {MASTER_KEY}"},
                timeout=2.0,
            )
            if resp.status_code == 200:
                ready = True
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.5)

    if not ready:
        proc.terminate()
        pytest.fail(f"litellm did not become ready in 60s:\n{''.join(captured)[-2000:]}")

    try:
        yield base, MASTER_KEY
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _models(base: str, key: str) -> set[str]:
    resp = httpx.get(f"{base}/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=5.0)
    assert resp.status_code == 200
    return {entry["id"] for entry in resp.json()["data"]}


def test_models_endpoint_returns_exactly_the_active_public_set(proxy: tuple[str, str]) -> None:
    base, key = proxy
    assert _models(base, key) == EXPECTED_ACTIVE


def test_public_names_have_no_provider_prefix(proxy: tuple[str, str]) -> None:
    base, key = proxy
    for model_id in _models(base, key):
        assert "/" not in model_id
        assert not model_id.startswith("chatgpt")


def test_missing_master_key_is_rejected(proxy: tuple[str, str]) -> None:
    base, _ = proxy
    # No Authorization header: the proxy must not serve the model list.
    resp = httpx.get(f"{base}/v1/models", timeout=5.0)
    assert resp.status_code != 200


def test_wrong_master_key_is_rejected(proxy: tuple[str, str]) -> None:
    base, _ = proxy
    resp = httpx.get(
        f"{base}/v1/models",
        headers={"Authorization": "Bearer sk-agw-not-the-key"},
        timeout=5.0,
    )
    assert resp.status_code != 200
