"""Reasoning-engine interface shared by all backends (SRS 5.2 / 8.4).

The contract is identical across backends; only the client and endpoint differ.
Each engine turns a (system, user) prompt pair into the model's text reply.
"""

from __future__ import annotations

from typing import Protocol


class Engine(Protocol):
    def complete(self, system: str, user: str) -> str:
        """Return the model's text completion for the given prompts."""
        ...
