"""Pluggable reasoning engines (SRS Section 8).

One interface, multiple backends selected by config alone (FR-11, NFR-9):
  summarize(messages) -> list[Summary]

  - base.py    interface / abstract contract
  - hosted.py  Anthropic Messages API or DashScope (Qwen)
  - local.py   OpenAI-compatible endpoint (Ollama / vLLM), no per-token cost
"""
