"""Phase 2 acceptance — correctly grouped, name-resolved messages (FR-1..FR-6)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from standup_summarizer.config import SlackConfig
from standup_summarizer.fetch import fetch_grouped_messages

from tests.conftest import FakeSlackClient, user

# Run window: a wide span that contains all the in-window test timestamps below.
OLDEST = datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc)
LATEST = datetime(2026, 6, 16, 23, 59, tzinfo=timezone.utc)

# Timestamps derived from the window so they stay consistent with it.
_BASE = OLDEST.timestamp()


def _ts(offset_seconds: float) -> str:
    return f"{_BASE + offset_seconds:.6f}"


TS_IN_1 = _ts(3600)  # one hour into the window
TS_IN_2 = _ts(7200)  # two hours into the window
TS_OUT = _ts(-86400)  # a day before the window


def _msg(user_id: str, ts: str, text: str, **extra) -> dict:
    return {"user": user_id, "ts": ts, "text": text, **extra}


def _config(include_threads: bool = True) -> SlackConfig:
    return SlackConfig(
        bot_token="xoxb-test", channel_id="C123", include_threads=include_threads
    )


def _fetch(client, include_threads=True):
    return fetch_grouped_messages(_config(include_threads), OLDEST, LATEST, client=client)


def test_groups_by_person_and_resolves_names():
    """FR-2, FR-3: messages keyed by person with resolved display names."""
    client = FakeSlackClient(
        messages=[
            _msg("U001", TS_IN_1, "Finished login API."),
            _msg("U002", TS_IN_2, "Blocked on DB creds."),
            _msg("U001", TS_IN_2, "Starting dashboard next."),
        ],
        users={
            "U001": user("U001", display_name="Alice"),
            "U002": user("U002", real_name="Bob Silva"),
        },
    )

    grouped = _fetch(client)

    assert set(grouped) == {"U001", "U002"}
    assert grouped["U001"].display_name == "Alice"
    assert grouped["U002"].display_name == "Bob Silva"
    # FR-3: order preserved within a person.
    assert [m.text for m in grouped["U001"].messages] == [
        "Finished login API.",
        "Starting dashboard next.",
    ]


def test_excludes_messages_outside_the_run_window():
    client = FakeSlackClient(
        messages=[
            _msg("U001", TS_IN_1, "today"),
            _msg("U001", TS_OUT, "yesteryear"),
        ],
        users={"U001": user("U001", display_name="Alice")},
    )

    grouped = _fetch(client)

    assert [m.text for m in grouped["U001"].messages] == ["today"]


def test_excludes_bot_and_system_messages():
    """FR-5: drop bot messages and subtyped system messages."""
    client = FakeSlackClient(
        messages=[
            _msg("U001", TS_IN_1, "real update"),
            _msg("U999", TS_IN_2, "automated", bot_id="B1"),
            _msg("U001", TS_IN_2, "joined", subtype="channel_join"),
        ],
        users={"U001": user("U001", display_name="Alice")},
    )

    grouped = _fetch(client)

    assert set(grouped) == {"U001"}
    assert [m.text for m in grouped["U001"].messages] == ["real update"]


def test_empty_channel_returns_empty(make_client):
    """FR-6: no messages -> empty mapping, not an error."""
    grouped = _fetch(make_client())
    assert grouped == {}


def test_pagination_collects_all_pages():
    """FR-1: more messages than one page are all collected."""
    msgs = [_msg("U001", _ts(60 * i), f"m{i}") for i in range(5)]
    client = FakeSlackClient(
        messages=msgs,
        users={"U001": user("U001", display_name="Alice")},
        page_size=2,
    )

    grouped = _fetch(client)

    assert len(grouped["U001"].messages) == 5


def test_thread_replies_included_when_flag_on():
    """FR-4: thread replies are folded into the author's standup."""
    parent = _msg("U001", TS_IN_1, "parent", thread_ts=TS_IN_1, reply_count=1)
    reply = _msg("U002", TS_IN_2, "reply in thread", thread_ts=TS_IN_1)
    client = FakeSlackClient(
        messages=[parent],
        users={
            "U001": user("U001", display_name="Alice"),
            "U002": user("U002", display_name="Bob"),
        },
        replies={TS_IN_1: [parent, reply]},
    )

    grouped = _fetch(client, include_threads=True)

    assert set(grouped) == {"U001", "U002"}
    assert grouped["U002"].messages[0].is_thread_reply is True


def test_thread_replies_excluded_when_flag_off():
    parent = _msg("U001", TS_IN_1, "parent", thread_ts=TS_IN_1, reply_count=1)
    reply = _msg("U002", TS_IN_2, "reply in thread", thread_ts=TS_IN_1)
    client = FakeSlackClient(
        messages=[parent],
        users={"U001": user("U001", display_name="Alice")},
        replies={TS_IN_1: [parent, reply]},
    )

    grouped = _fetch(client, include_threads=False)

    assert set(grouped) == {"U001"}
