"""Pluggable reasoning engines (SRS Section 8).

One interface (engines/base.py: Engine.complete), multiple backends selected by
config alone (FR-11, NFR-9):

  - local.py   OpenAI-compatible endpoint (Ollama / vLLM / OpenAI / DashScope)
  - hosted.py  Anthropic Messages API

Use ``build_engine(cfg)`` to get the engine for the configured backend.
"""

from __future__ import annotations

from ..config import ReasoningConfig
from .base import Engine

# OpenAI-compatible backends (no special Anthropic SDK).
_OPENAI_COMPATIBLE = {"local", "openai", "dashscope", "vllm", "ollama"}


def build_engine(cfg: ReasoningConfig) -> Engine:
    """Construct the reasoning engine for the configured backend."""
    if cfg.backend == "anthropic":
        from .hosted import AnthropicEngine

        return AnthropicEngine(cfg.model, cfg.api_key)
    if cfg.backend in _OPENAI_COMPATIBLE:
        from .local import OpenAICompatibleEngine

        return OpenAICompatibleEngine(cfg.model, cfg.base_url, cfg.api_key)
    raise ValueError(
        f"Unknown REASONING_BACKEND {cfg.backend!r}; "
        f"use 'anthropic' or one of {sorted(_OPENAI_COMPATIBLE)}."
    )


__all__ = ["Engine", "build_engine"]
