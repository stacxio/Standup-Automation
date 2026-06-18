"""Shared pytest fixtures.

Each module must be unit-testable against fixtures without live API calls (NFR-8).
Provides a FakeSlackClient implementing the slice of slack_sdk.WebClient that
fetch.py depends on, with pagination, window filtering and user lookup.
"""

from __future__ import annotations

from typing import Any

import pytest


class FakeSlackClient:
    """In-memory stand-in for slack_sdk.WebClient."""

    def __init__(
        self,
        messages: list[dict] | None = None,
        users: dict[str, dict] | None = None,
        replies: dict[str, list[dict]] | None = None,
        page_size: int = 200,
    ) -> None:
        self.messages = messages or []
        self.users = users or {}
        self.replies = replies or {}
        self.page_size = page_size

    def conversations_history(
        self,
        channel: str,
        oldest: str = "0",
        latest: str = "9999999999",
        inclusive: bool = True,
        limit: int = 200,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        in_window = [
            m for m in self.messages if float(oldest) <= float(m["ts"]) <= float(latest)
        ]
        return self._paginate(in_window, cursor)

    def conversations_replies(
        self,
        channel: str,
        ts: str,
        limit: int = 200,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        thread = self.replies.get(ts, [])
        return self._paginate(thread, cursor)

    def users_info(self, user: str) -> dict[str, Any]:
        if user not in self.users:
            raise KeyError(user)
        return {"user": self.users[user]}

    def _paginate(self, items: list[dict], cursor: str | None) -> dict[str, Any]:
        start = int(cursor) if cursor else 0
        page = items[start : start + self.page_size]
        next_start = start + self.page_size
        next_cursor = str(next_start) if next_start < len(items) else ""
        return {
            "messages": page,
            "response_metadata": {"next_cursor": next_cursor},
        }


def user(user_id: str, display_name: str = "", real_name: str = "") -> dict:
    """Build a users_info-style user record."""
    return {
        "id": user_id,
        "name": user_id.lower(),
        "real_name": real_name,
        "profile": {"display_name": display_name, "real_name": real_name},
    }


@pytest.fixture
def make_client():
    return FakeSlackClient
