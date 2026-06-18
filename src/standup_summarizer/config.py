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
