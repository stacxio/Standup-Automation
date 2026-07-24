"""Prefix-based channel routing for the daily summary (daily_summary + config).

Each developer's tasks are split by issue-key prefix and sent to that project's
channel; unrouted prefixes and task-less developers fall back to the default
channel (SLACK_CHANNEL_ID).
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import daily_summary as ds  # noqa: E402
from standup_summarizer.config import SlackConfig, _parse_routes  # noqa: E402

TODAY = dt.date(2026, 7, 23)
DEFAULT = "Cdefault"


def _cfg() -> SlackConfig:
    return SlackConfig(
        bot_token="x",
        channel_id=DEFAULT,
        channel_routes={"SP": "Cstacx", "WS": "Cstacx", "HIR": "Chiro", "BHA": "Cbha"},
    )


def _row(name, checkin, previous, picked, done):
    return {
        "name": name,
        "checkin": checkin,
        "previous": previous,
        "picked": picked,
        "done": done,
        "jira": [[f"{k}: summary of {k}"] for k in picked],
        "comments": [f"{k}: no comments" for k in picked],
    }


# --- config parsing -------------------------------------------------------
def test_parse_routes_skips_blank_and_malformed():
    routes = _parse_routes("SP:C1, WS:C1 ,HIR:C2,,bad,: ,BHA:C3")
    assert routes == {"SP": "C1", "WS": "C1", "HIR": "C2", "BHA": "C3"}


def test_parse_routes_empty():
    assert _parse_routes("") == {}
    assert _parse_routes(None) == {}


def test_channel_for_prefix_is_case_insensitive_with_default():
    cfg = _cfg()
    assert cfg.channel_for_prefix("hir") == "Chiro"
    assert cfg.channel_for_prefix("HIR") == "Chiro"
    assert cfg.channel_for_prefix("ST") == DEFAULT  # unrouted -> default
    assert cfg.channel_for_prefix("") == DEFAULT


def test_prefix_of():
    assert ds.prefix_of("HIR-72") == "HIR"
    assert ds.prefix_of("sp-9") == "SP"
    assert ds.prefix_of("WS-186") == "WS"
    assert ds.prefix_of("NOHYPHEN") == "NOHYPHEN"


# --- routing --------------------------------------------------------------
def test_mixed_prefix_developer_is_split_across_channels():
    cfg = _cfg()
    soma = _row("Soma", ds.CHECKIN_DONE, ["SP-9"], ["SP-11", "ST-11", "WS-3"], [])
    routed = ds.route_blocks([soma], cfg, TODAY, jira_configured=True)

    # SP + WS route to #stacx; ST is unrouted -> default.
    assert set(routed) == {"Cstacx", DEFAULT}

    stacx = "\n".join(routed["Cstacx"])
    assert "Task picked: SP-11, WS-3" in stacx
    assert "Previous task: SP-9" in stacx
    assert "ST-11" not in stacx  # the ST slice must not leak here

    default = "\n".join(routed[DEFAULT])
    assert "Task picked: ST-11" in default
    assert "SP-11" not in default and "WS-3" not in default


def test_header_is_first_block_per_channel():
    cfg = _cfg()
    routed = ds.route_blocks([_row("R", ds.CHECKIN_DONE, [], ["HIR-72"], ["HIR-72"])],
                             cfg, TODAY, jira_configured=True)
    for blocks in routed.values():
        assert blocks[0].startswith("*Daily Stand-up Summary")


def test_taskless_developer_falls_back_to_default_channel():
    cfg = _cfg()
    idle = _row("Idle", ds.CHECKIN_NONE, [], [], [])
    routed = ds.route_blocks([idle], cfg, TODAY, jira_configured=True)
    assert set(routed) == {DEFAULT}
    body = "\n".join(routed[DEFAULT])
    assert "*Idle*" in body and "status: No update from developer" in body


def test_developer_routed_by_previous_task_only():
    """No picked task today, but a previous HIR task -> appears in #hiro."""
    cfg = _cfg()
    row = _row("Raghul", ds.CHECKIN_DONE, ["HIR-53"], [], [])
    routed = ds.route_blocks([row], cfg, TODAY, jira_configured=True)
    assert set(routed) == {"Chiro"}
    body = "\n".join(routed["Chiro"])
    assert "Previous task: HIR-53" in body
    assert "no task picked today" in body  # picked slice is empty here


def test_done_slice_is_filtered_per_channel():
    cfg = _cfg()
    row = _row("Dev", ds.CHECKIN_DONE, [], ["HIR-1", "SP-2"], ["HIR-1", "SP-2"])
    routed = ds.route_blocks([row], cfg, TODAY, jira_configured=True)
    assert "Task done: HIR-1" in "\n".join(routed["Chiro"])
    assert "Task done: SP-2" in "\n".join(routed["Cstacx"])


def test_no_routes_configured_sends_everything_to_default():
    cfg = SlackConfig(bot_token="x", channel_id=DEFAULT, channel_routes={})
    rows = [_row("A", ds.CHECKIN_DONE, [], ["HIR-1", "SP-2"], [])]
    routed = ds.route_blocks(rows, cfg, TODAY, jira_configured=True)
    assert set(routed) == {DEFAULT}
