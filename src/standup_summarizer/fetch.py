"""Fetch stage — Slack channel history for the run window (FR-1..FR-6).

- Retrieve all messages in the run window, handling pagination (FR-1).
- Resolve user IDs to display/real names (FR-2).
- Group by author, preserving order and timestamps (FR-3).
- Include/exclude thread replies per config flag, default include (FR-4).
- Exclude bot and system messages (FR-5).
- Empty channel -> empty set, not a failure (FR-6).

The fetcher takes any object with the slack_sdk ``WebClient`` surface, so it is
unit-testable against a fake without live API calls (NFR-8). Use
``build_client()`` for the real thing.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Protocol

from .config import SlackConfig
from .models import GroupedMessages, Message, PersonStandup

log = logging.getLogger(__name__)

# Slack returns at most this many items per page; we paginate past it (FR-1).
_PAGE_SIZE = 200
# How many times to retry a rate-limited (HTTP 429) call before giving up.
_MAX_RETRIES = 3


class SlackClient(Protocol):
    """The subset of slack_sdk.WebClient that this module uses."""

    def conversations_history(self, **kwargs: Any) -> Any: ...
    def conversations_replies(self, **kwargs: Any) -> Any: ...
    def users_info(self, **kwargs: Any) -> Any: ...


def build_client(token: str) -> SlackClient:
    """Create a real Slack WebClient (imported lazily so tests need no SDK)."""
    from slack_sdk import WebClient

    return WebClient(token=token)


def fetch_grouped_messages(
    config: SlackConfig,
    oldest: datetime,
    latest: datetime,
    client: SlackClient | None = None,
) -> GroupedMessages:
    """Fetch the run window's messages and group them by person (FR-1..FR-6).

    Args:
        config: Slack settings (token, channel, include-threads flag).
        oldest: inclusive start of the run window (timezone-aware).
        latest: inclusive end of the run window (timezone-aware).
        client: an injected Slack client; built from the token when omitted.

    Returns:
        A mapping of user id -> ``PersonStandup``, empty if nobody posted.
    """
    client = client or build_client(config.bot_token)
    fetcher = _SlackFetcher(client)

    oldest_ts = _to_slack_ts(oldest)
    latest_ts = _to_slack_ts(latest)

    raw = fetcher.channel_history(config.channel_id, oldest_ts, latest_ts)

    if config.include_threads:
        raw += fetcher.thread_replies(config.channel_id, raw, oldest_ts, latest_ts)

    kept = [m for m in raw if _is_standup_message(m)]
    grouped = fetcher.group_by_person(kept)

    log.info(
        "Fetched %d standup message(s) from %d people in channel %s",
        len(kept),
        len(grouped),
        config.channel_id,
    )
    return grouped


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


class _SlackFetcher:
    def __init__(self, client: SlackClient) -> None:
        self._client = client
        self._name_cache: dict[str, str] = {}

    # FR-1: paginated channel history bounded by the run window.
    def channel_history(self, channel: str, oldest: str, latest: str) -> list[dict]:
        messages: list[dict] = []
        cursor: str | None = None
        while True:
            resp = self._call(
                self._client.conversations_history,
                channel=channel,
                oldest=oldest,
                latest=latest,
                inclusive=True,
                limit=_PAGE_SIZE,
                cursor=cursor,
            )
            messages.extend(resp.get("messages", []))
            cursor = (resp.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                break
        return messages

    # FR-4: thread replies, only for parents that actually have a thread.
    def thread_replies(
        self, channel: str, parents: list[dict], oldest: str, latest: str
    ) -> list[dict]:
        replies: list[dict] = []
        for parent in parents:
            thread_ts = parent.get("thread_ts")
            # A thread parent has thread_ts == its own ts and a reply_count > 0.
            if not thread_ts or thread_ts != parent.get("ts"):
                continue
            if not parent.get("reply_count"):
                continue
            cursor: str | None = None
            while True:
                resp = self._call(
                    self._client.conversations_replies,
                    channel=channel,
                    ts=thread_ts,
                    limit=_PAGE_SIZE,
                    cursor=cursor,
                )
                for msg in resp.get("messages", []):
                    # The first item is the parent (already collected); skip it,
                    # and keep only replies inside the run window.
                    if msg.get("ts") == thread_ts:
                        continue
                    if oldest <= msg.get("ts", "0") <= latest:
                        msg["_is_thread_reply"] = True
                        replies.append(msg)
                cursor = (resp.get("response_metadata") or {}).get("next_cursor") or None
                if not cursor:
                    break
        return replies

    # FR-2 + FR-3: resolve names and group, preserving chronological order.
    def group_by_person(self, messages: list[dict]) -> GroupedMessages:
        grouped: GroupedMessages = {}
        for msg in sorted(messages, key=lambda m: m.get("ts", "0")):
            user_id = msg["user"]
            if user_id not in grouped:
                grouped[user_id] = PersonStandup(
                    user_id=user_id,
                    display_name=self._resolve_name(user_id),
                )
            grouped[user_id].messages.append(
                Message(
                    ts=msg["ts"],
                    text=msg.get("text", ""),
                    user_id=user_id,
                    is_thread_reply=bool(msg.get("_is_thread_reply")),
                )
            )
        return grouped

    # FR-2: user id -> human name, cached so each id is looked up once.
    def _resolve_name(self, user_id: str) -> str:
        if user_id in self._name_cache:
            return self._name_cache[user_id]
        try:
            resp = self._call(self._client.users_info, user=user_id)
            profile = (resp.get("user") or {}).get("profile") or {}
            user = resp.get("user") or {}
            name = (
                profile.get("display_name")
                or profile.get("real_name")
                or user.get("real_name")
                or user.get("name")
                or user_id
            )
        except Exception:  # noqa: BLE001 — never let name lookup fail the run
            log.warning("Could not resolve name for user %s; using id", user_id)
            name = user_id
        self._name_cache[user_id] = name
        return name

    # Thin wrapper that retries on Slack rate limiting (HTTP 429).
    def _call(self, method: Any, **kwargs: Any) -> Any:
        for attempt in range(_MAX_RETRIES):
            try:
                return method(**{k: v for k, v in kwargs.items() if v is not None})
            except Exception as exc:  # noqa: BLE001
                retry_after = _rate_limit_delay(exc)
                if retry_after is None or attempt == _MAX_RETRIES - 1:
                    raise
                log.warning("Slack rate-limited; retrying in %ss", retry_after)
                time.sleep(retry_after)
        raise RuntimeError("unreachable")  # pragma: no cover


def _rate_limit_delay(exc: Exception) -> int | None:
    """Return the Retry-After seconds if ``exc`` is a 429, else None."""
    response = getattr(exc, "response", None)
    if response is None or getattr(response, "status_code", None) != 429:
        return None
    headers = getattr(response, "headers", {}) or {}
    try:
        return int(headers.get("Retry-After", 1))
    except (TypeError, ValueError):
        return 1


def _is_standup_message(msg: dict) -> bool:
    """Keep only real human messages (FR-5): no bots, no system subtypes."""
    if msg.get("bot_id") or msg.get("subtype"):
        return False
    return bool(msg.get("user")) and bool(msg.get("text", "").strip())


def _to_slack_ts(dt: datetime) -> str:
    """Convert a datetime to a Slack timestamp string (Unix epoch seconds)."""
    return f"{dt.timestamp():.6f}"
