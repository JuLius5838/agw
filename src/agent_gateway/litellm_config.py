"""Deterministic rendering of the LiteLLM proxy config from the model registry.

Given a validated :class:`ModelRegistry`, produce a byte-for-byte stable LiteLLM
``model_list`` YAML that exposes each active public name unchanged as its
``model_name`` and routes it to the provider-prefixed upstream. Logging is
configured conservatively so LiteLLM never persists prompt/response bodies by
default. The proxy key is NEVER written here — it is supplied to LiteLLM through
its process environment (``LITELLM_MASTER_KEY``) by the supervisor.
"""

from __future__ import annotations

import hashlib

import yaml

from agent_gateway.model_registry import ModelRegistry
from agent_gateway.paths import SECRET_FILE_MODE, Paths, atomic_write_text, ensure_dir


def render_litellm_config(registry: ModelRegistry) -> str:
    """Render a deterministic LiteLLM proxy YAML config for the active models."""
    model_list = [
        {
            "model_name": entry.name,
            "litellm_params": {"model": entry.upstream_model},
            "model_info": {"mode": entry.mode.value},
        }
        # active_models() is already sorted by public name for determinism.
        for entry in registry.active_models()
    ]

    document = {
        "model_list": model_list,
        "litellm_settings": {
            # Translate/permit cross-provider params instead of erroring.
            "drop_params": True,
            # Never log message content through LiteLLM's own callbacks.
            "turn_off_message_logging": True,
        },
        "general_settings": {
            # Do not persist spend/usage rows locally.
            "disable_spend_logs": True,
        },
    }
    # sort_keys=True makes every mapping deterministic; the list order is already
    # fixed by active_models(), so the whole document is byte-for-byte stable.
    return yaml.safe_dump(document, sort_keys=True, default_flow_style=False, allow_unicode=True)


def config_fingerprint(rendered: str) -> str:
    """Stable SHA-256 fingerprint of a rendered config, used to detect drift."""
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def write_litellm_config(paths: Paths, registry: ModelRegistry) -> str:
    """Render and atomically write the generated LiteLLM config; return its text."""
    ensure_dir(paths.state_dir)
    rendered = render_litellm_config(registry)
    atomic_write_text(paths.generated_litellm_config, rendered, mode=SECRET_FILE_MODE)
    return rendered
