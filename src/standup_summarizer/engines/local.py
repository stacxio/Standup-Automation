"""OpenAI-compatible reasoning backend.

Works against any OpenAI-compatible /v1 endpoint — Ollama or vLLM running an
open-weight model locally (zero token cost, content stays on owned hardware,
SRS 8.1 / NFR-7), or a hosted OpenAI-compatible API (OpenAI, Alibaba DashScope)
when an api_key/base_url is supplied.
"""

from __future__ import annotations


class OpenAICompatibleEngine:
    def __init__(self, model: str, base_url: str | None, api_key: str | None) -> None:
        from openai import OpenAI

        # Ollama/vLLM ignore the key but the client requires a non-empty string.
        self._client = OpenAI(base_url=base_url or None, api_key=api_key or "not-needed")
        self._model = model

    def complete(self, system: str, user: str) -> str:
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0,
        )
        return resp.choices[0].message.content or ""
