"""Load settings and secrets from the environment; expose typed config.

All credentials are read from env / secret files — never hardcoded (NFR-5).
Only the Slack settings are defined here so far; reasoning-engine and Sheets
config will be added alongside their modules.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str | None = None, *, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class SlackConfig:
    """Settings for the fetch stage (SRS Section 9 — Slack)."""

    bot_token: str
    channel_id: str
    include_threads: bool = True

    @classmethod
    def from_env(cls) -> "SlackConfig":
        return cls(
            bot_token=_env("SLACK_BOT_TOKEN", required=True),  # type: ignore[arg-type]
            channel_id=_env("SLACK_CHANNEL_ID", required=True),  # type: ignore[arg-type]
            include_threads=_env_bool("SLACK_INCLUDE_THREADS", True),
        )


@dataclass(frozen=True)
class JiraConfig:
    """Atlassian Cloud REST credentials, used to check issue status categories.

    Optional: from_env() returns None when any of the three vars is missing, so
    the report still runs (Picked Tasks stays populated from Slack; Completed
    Tasks is simply left blank).
    """

    base_url: str
    email: str
    api_token: str

    @classmethod
    def from_env(cls) -> "JiraConfig | None":
        base = _env("JIRA_BASE_URL")
        email = _env("JIRA_EMAIL")
        token = _env("JIRA_API_TOKEN")
        if not (base and email and token):
            return None
        return cls(base_url=base.rstrip("/"), email=email, api_token=token)


@dataclass(frozen=True)
class ReasoningConfig:
    """Settings for the summarize stage's reasoning engine (SRS Section 8 / 9).

    backend selects the engine; everything else is config, not code (FR-11):
      * "local"  / "openai" / "dashscope" -> OpenAI-compatible endpoint
      * "anthropic"                        -> Anthropic Messages API
    """

    backend: str
    model: str
    base_url: str | None = None
    api_key: str | None = None
    max_retries: int = 3

    @classmethod
    def from_env(cls) -> "ReasoningConfig":
        return cls(
            backend=(_env("REASONING_BACKEND", "local") or "local").lower(),
            model=_env("REASONING_MODEL", "llama3.2"),  # type: ignore[arg-type]
            base_url=_env("REASONING_BASE_URL", "http://localhost:11434/v1"),
            api_key=_env("REASONING_API_KEY"),
            max_retries=int(_env("REASONING_MAX_RETRIES", "3") or 3),
        )
