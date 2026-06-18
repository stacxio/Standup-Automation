"""Local reasoning backend — OpenAI-compatible endpoint, zero token cost.

Ollama or vLLM serving an open-weight Qwen3 model at a configurable base URL,
so message content stays on owned hardware (SRS 8.1, NFR-7).
"""
