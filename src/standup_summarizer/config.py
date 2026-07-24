"""Load settings and secrets from the environment; expose typed config.

All credentials are read from env / secret files — never hardcoded (NFR-5).
Only the Slack settings are defined here so far; reasoning-engine and Sheets
config will be added alongside their modules.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


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


def _parse_routes(raw: str | None) -> dict[str, str]:
    """Parse 'SP:C0AAA,WS:C0AAA,HIR:C0BBB' into {prefix_upper: channel}.

    Blank entries and malformed pairs (missing ':') are skipped so a typo in
    one route never breaks the whole run. Prefixes are upper-cased so matching
    is case-insensitive; channel ids/names are taken verbatim.
    """
    routes: dict[str, str] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        prefix, channel = part.split(":", 1)
        prefix, channel = prefix.strip().upper(), channel.strip()
        if prefix and channel:
            routes[prefix] = channel
    return routes


@dataclass(frozen=True)
class SlackConfig:
    """Settings for the fetch stage (SRS Section 9 — Slack).

    `channel_id` is the default channel (SLACK_CHANNEL_ID — #stacx-check-in);
    it is where fetches read from and where any summary slice with no matching
    route falls back to. `channel_routes` maps an issue-key prefix (e.g. "HIR")
    to the coordination channel that prefix's summary should post to.
    """

    bot_token: str
    channel_id: str
    include_threads: bool = True
    channel_routes: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "SlackConfig":
        return cls(
            bot_token=_env("SLACK_BOT_TOKEN", required=True),  # type: ignore[arg-type]
            channel_id=_env("SLACK_CHANNEL_ID", required=True),  # type: ignore[arg-type]
            include_threads=_env_bool("SLACK_INCLUDE_THREADS", True),
            channel_routes=_parse_routes(_env("SUMMARY_CHANNEL_ROUTES")),
        )

    def channel_for_prefix(self, prefix: str) -> str:
        """Channel for an issue-key prefix, or the default channel if unrouted."""
        return self.channel_routes.get((prefix or "").upper(), self.channel_id)


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
