"""Agent Gateway — add subscription-backed external models to Claude Code.

Claude Code talks to a per-user hybrid front router. Native Claude requests keep
Claude Code's saved login and go directly to Anthropic; configured external model
names route to a private LiteLLM child using ChatGPT/Codex OAuth.
"""

from __future__ import annotations

__version__ = "0.1.9"
