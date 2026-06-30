"""Hosted reasoning backend — Anthropic Messages API (pay-per-token, API key).

(Hosted OpenAI-compatible APIs such as Alibaba DashScope are served by the
OpenAICompatibleEngine in local.py with a base_url + api_key.)
"""

from __future__ import annotations


class AnthropicEngine:
    def __init__(self, model: str, api_key: str | None, max_tokens: int = 4096) -> None:
        from anthropic import Anthropic

        self._client = Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    def complete(self, system: str, user: str) -> str:
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            temperature=0,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(block.text for block in resp.content if block.type == "text")
