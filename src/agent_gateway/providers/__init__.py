"""External provider identities and their LiteLLM routing prefixes.

This is a dependency-light leaf module (stdlib only) so both the CLI and the model
registry can import :class:`Provider` without pulling in heavier modules. The
concrete authentication adapter lives alongside it in ``chatgpt.py``.
"""

from __future__ import annotations

from enum import StrEnum


class Provider(StrEnum):
    """A supported external subscription provider.

    Claude is deliberately absent: native Claude requests are forwarded directly
    to Anthropic with Claude Code's own saved subscription credential.
    """

    chatgpt = "chatgpt"


# The mandatory LiteLLM model prefix for each provider. A model's ``upstream_model``
# must start with its provider's prefix; the prefix is internal routing detail and
# never appears in a public model name given to Claude Code.
PROVIDER_PREFIX: dict[Provider, str] = {
    Provider.chatgpt: "chatgpt/",
}
