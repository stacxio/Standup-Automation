"""Tests for the fetch CLI — window computation, output, and exit codes."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from standup_summarizer import cli
from standup_summarizer.models import Message, PersonStandup


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    """Don't let a real .env on disk leak into CLI tests."""
    monkeypatch.setattr(cli, "_load_dotenv", lambda: None)


@pytest.fixture
def _creds(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C123")
    monkeypatch.delenv("SLACK_INCLUDE_THREADS", raising=False)


def _grouped():
    return {
        "U001": PersonStandup(
            user_id="U001",
            display_name="Alice",
            messages=[Message(ts="1781568000.000100", text="Shipped login.", user_id="U001")],
        )
    }


# --------------------------------------------------------------------------
# day_window
# --------------------------------------------------------------------------


def test_day_window_covers_full_local_day():
    tz = ZoneInfo("UTC")
    start, end = cli.day_window(datetime(2026, 6, 17), tz)

    assert start == datetime(2026, 6, 17, 0, 0, 0, tzinfo=tz)
    assert end == datetime(2026, 6, 17, 23, 59, 59, 999999, tzinfo=tz)
    assert end - start == timedelta(days=1) - timedelta(microseconds=1)


def test_day_window_respects_timezone_offset():
    tz = ZoneInfo("Asia/Colombo")  # +05:30, no DST
    start, _ = cli.day_window(datetime(2026, 6, 17), tz)

    assert start.utcoffset() == timedelta(hours=5, minutes=30)
    # Midnight local is 18:30 the previous day in UTC.
    assert start.astimezone(ZoneInfo("UTC")).hour == 18


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_fetch_human_output(monkeypatch, capsys, _creds):
    monkeypatch.setattr(cli, "fetch_grouped_messages", lambda *a, **k: _grouped())

    rc = cli.main(["fetch", "--date", "2026-06-17", "--tz", "UTC"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "Run window:" in out
    assert "Alice (U001)" in out
    assert "Shipped login." in out


def test_fetch_json_output(monkeypatch, capsys, _creds):
    monkeypatch.setattr(cli, "fetch_grouped_messages", lambda *a, **k: _grouped())

    rc = cli.main(["fetch", "--date", "2026-06-17", "--tz", "UTC", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["U001"]["display_name"] == "Alice"
    assert payload["U001"]["messages"][0]["text"] == "Shipped login."


def test_fetch_empty_window(monkeypatch, capsys, _creds):
    monkeypatch.setattr(cli, "fetch_grouped_messages", lambda *a, **k: {})

    rc = cli.main(["fetch", "--tz", "UTC"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "No standup messages found" in out


# --------------------------------------------------------------------------
# Error exit codes
# --------------------------------------------------------------------------


def test_missing_credentials_exits_2(monkeypatch, capsys):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_CHANNEL_ID", raising=False)

    rc = cli.main(["fetch", "--tz", "UTC"])

    err = capsys.readouterr().err
    assert rc == 2
    assert "SLACK_BOT_TOKEN" in err


def test_bad_timezone_exits_2(capsys, _creds):
    rc = cli.main(["fetch", "--tz", "Mars/Phobos"])

    err = capsys.readouterr().err
    assert rc == 2
    assert "Unknown timezone" in err


def test_bad_date_exits_2(capsys, _creds):
    rc = cli.main(["fetch", "--date", "17-06-2026", "--tz", "UTC"])

    err = capsys.readouterr().err
    assert rc == 2
    assert "Invalid --date" in err


def test_slack_failure_exits_1(monkeypatch, capsys, _creds):
    def _boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(cli, "fetch_grouped_messages", _boom)

    rc = cli.main(["fetch", "--tz", "UTC"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "Slack fetch failed" in err
