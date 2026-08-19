"""Prefix-based channel routing for the daily summary (daily_summary + config).

Each developer's tasks are split by issue-key prefix and sent to that project's
channel; unrouted prefixes and task-less developers fall back to the default
channel (SLACK_CHANNEL_ID).
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

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


# --- SCM facts when Jira has no SCM integration ---------------------------
# The development panel is populated only by a real GitHub/Bitbucket link. This
# Jira has none, so the panel is empty on every issue while the team pastes the
# branch, commit and PR into a comment instead.
class _FakeLook:
    """The slice of _JiraLookup that issue_block uses."""

    def __init__(self, *, dev=None, comments=(), summary="Do the thing",
                 description="x" * 50):
        self._dev = dev or {"branches": [], "commits": [], "pull_requests": []}
        self._comments = list(comments)
        self._detail = {"id": "1", "key": "HIR-91", "summary": summary,
                        "description": description, "status_name": "In Progress",
                        "status_category": "indeterminate", "issue_type": "Task",
                        "parent_key": "", "assignee": "Raghul", "assignee_id": "",
                        "priority": "Medium", "attachments": []}

    def detail(self, key):
        return self._detail

    def dev_info(self, key):
        return self._dev

    def comments(self, key):
        return self._comments


def _comment(body: str, author: str = "Raghul") -> dict:
    return {"author": author, "created": "2026-08-18T10:00:00.000+0530",
            "body": body, "media": []}


def _lines(look) -> dict:
    """issue_block output as {label: value}."""
    out = {}
    for line in ds.issue_block("HIR-91", look)[1:]:
        label, _, value = line.partition(": ")
        out[label] = value
    return out


# The real HIR-91 comment that prompted this.
REAL = ("Latest branch: main GitHub: agb-admin:  agb:  Live URL: "
        "http://95.217.232.107:5004/prices Path: Monitor -> Prices "
        "Latest commits: agb-admin: 2934bb5 agb: 11f33ea")


def test_commit_reads_yes_when_ids_are_named_in_comments():
    got = _lines(_FakeLook(comments=[_comment(REAL)]))
    assert got["commit"] == f"{ds.LINKED} {ds.FROM_COMMENTS}"


def test_the_fallback_is_labelled_so_the_source_is_never_ambiguous():
    assert ds.FROM_COMMENTS in _lines(_FakeLook(comments=[_comment(REAL)]))["commit"]


def test_the_dev_panel_wins_and_is_not_labelled():
    look = _FakeLook(dev={"branches": ["feat/x"], "commits": ["abc1234"],
                          "pull_requests": []},
                     comments=[_comment(REAL)])
    got = _lines(look)
    assert got["commit"] == ds.LINKED          # yes, and no "(from comments)"
    assert got["branch"] == "feat/x"


# --- a commit named by label + url ----------------------------------------
@pytest.mark.parametrize("body", [
    "Latest commit: https://github.com/o/r/commit/2934bb5",
    "Latest commit id: https://gitlab.com/o/r/-/tree/abc",
    "Latest commits: https://bitbucket.org/o/r/branches/compare/x..y",
    "commit id - https://github.com/o/r/compare/a...b",
    "commit https://short.link/xyz",
    "Commit ID: https://github.com/o/r/commit/2934bb5.",
])
def test_a_labelled_commit_url_is_picked_up(body):
    assert _lines(_FakeLook(comments=[_comment(body)]))["commit"].startswith(ds.LINKED)


@pytest.mark.parametrize("body", [
    "We commit to shipping this: https://docs.example.com/plan",
    "I will commit to the deadline. See https://example.com/notes",
    "Latest commit: coming tomorrow",          # label, no url
    "https://example.com/some/page",           # url, no label
])
def test_prose_around_the_word_commit_is_not_a_commit(body):
    assert _lines(_FakeLook(comments=[_comment(body)]))["commit"] == ds.NOT_LINKED


def test_a_labelled_commit_url_is_not_double_counted():
    from standup_summarizer import scorecard as sc

    ids = sc.commit_ids_in_text("Latest commit: https://github.com/o/r/commit/2934bb5")
    assert len(ids) == 1


def test_a_trailing_full_stop_is_not_part_of_the_url():
    from standup_summarizer import scorecard as sc

    ids = sc.commit_ids_in_text("Commit id: https://short.link/xyz.")
    assert ids == ["https://short.link/xyz"]


def test_a_pr_url_in_a_comment_is_picked_up_and_merge_state_is_not_guessed():
    look = _FakeLook(comments=[_comment("PR up: https://github.com/o/r/pull/42")])
    got = _lines(look)
    assert got["PR"] == "https://github.com/o/r/pull/42 (from comments)"
    assert got["PR merged"] == ds.UNKNOWN      # prose cannot tell us; "no" would be a claim


def test_prose_about_a_pr_without_a_url_is_not_a_pr():
    got = _lines(_FakeLook(comments=[_comment("I raised the PR for review.")]))
    assert got["PR"] == ds.NOT_LINKED


def test_branch_is_never_guessed_from_prose():
    """"Latest branch: main GitHub: ..." has no reliable shape to parse."""
    assert _lines(_FakeLook(comments=[_comment(REAL)]))["branch"] == ds.NOT_LINKED


def test_nothing_anywhere_still_reads_not_linked():
    got = _lines(_FakeLook(comments=[_comment("Working on it, no blockers yet.")]))
    assert got["commit"] == ds.NOT_LINKED
    assert got["PR"] == ds.NOT_LINKED
    assert got["PR merged"] == "no"


def test_a_false_positive_hex_word_is_not_reported_as_a_commit():
    got = _lines(_FakeLook(comments=[_comment("Fixed the facade and the deadbeef case.")]))
    assert got["commit"] == ds.NOT_LINKED


def test_the_digest_and_the_scorecard_agree_about_commits():
    """Both read scorecard.commit_ids_in_text, so they cannot diverge."""
    from standup_summarizer import scorecard as sc

    for body in (REAL, "Latest commit: https://github.com/o/r/commit/2934bb5",
                 "commit id - https://short.link/abc"):
        digest_found = _lines(_FakeLook(comments=[_comment(body)]))["commit"] != ds.NOT_LINKED
        assert sc.has_commit_reference(body) is digest_found


def test_issue_that_cannot_be_read_is_reported_not_crashed():
    class Gone(_FakeLook):
        def detail(self, key):
            return None

    assert "could not read" in ds.issue_block("HIR-91", Gone())[0]
