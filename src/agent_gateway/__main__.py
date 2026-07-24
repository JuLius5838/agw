"""Module entry point so ``python -m agent_gateway`` matches the ``agw`` console script."""

from __future__ import annotations

from agent_gateway.cli import app

if __name__ == "__main__":
    app()
