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
        "task_status": [f"{k}-In Progress" for k in picked],
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


def test_the_exact_commit_ids_from_the_comment_are_shown():
    """Verbatim, in the order the developer wrote them."""
    got = _lines(_FakeLook(comments=[_comment(REAL)]))
    assert got["commit"] == f"2934bb5, 11f33ea {ds.FROM_COMMENTS}"


def test_ids_are_not_shortened_or_reformatted():
    full = "a" * 3 + "1" * 37          # a 40-char sha
    got = _lines(_FakeLook(comments=[_comment(f"Latest commit: {full}")]))
    assert got["commit"].startswith(full)


def test_a_long_list_of_commits_is_capped_but_counted():
    ids = [f"{i}abc123" for i in range(9)]
    got = _lines(_FakeLook(comments=[_comment("Latest commits: " + " ".join(ids))]))
    assert got["commit"].startswith(", ".join(ids[:ds.MAX_COMMITS_SHOWN]))
    assert f"(+{9 - ds.MAX_COMMITS_SHOWN} more)" in got["commit"]


def test_exactly_the_cap_needs_no_more_marker():
    ids = [f"{i}abc123" for i in range(ds.MAX_COMMITS_SHOWN)]
    got = _lines(_FakeLook(comments=[_comment("Latest commits: " + " ".join(ids))]))
    assert "more)" not in got["commit"]


def test_the_fallback_is_labelled_so_the_source_is_never_ambiguous():
    assert ds.FROM_COMMENTS in _lines(_FakeLook(comments=[_comment(REAL)]))["commit"]


def test_the_dev_panel_wins_and_is_not_labelled():
    look = _FakeLook(dev={"branches": ["feat/x"], "commits": ["abc1234"],
                          "pull_requests": []},
                     comments=[_comment(REAL)])
    got = _lines(look)
    assert got["commit"] == "abc1234"          # panel id, no "(from comments)"
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
    """The url itself is the id shown — it is what the developer recorded."""
    commit = _lines(_FakeLook(comments=[_comment(body)]))["commit"]
    assert commit != ds.NOT_LINKED
    assert commit.startswith("https://") or commit.startswith("2934bb5")


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


def test_prose_about_a_pr_without_a_url_is_not_a_pr():
    got = _lines(_FakeLook(comments=[_comment("I raised the PR for review.")]))
    assert got["PR"] == ds.NOT_LINKED


def test_nothing_anywhere_still_reads_not_linked():
    got = _lines(_FakeLook(comments=[_comment("Working on it, no blockers yet.")]))
    assert got["commit"] == ds.NOT_LINKED
    assert got["PR"] == ds.NOT_LINKED
    assert got["PR merged"] == ds.MERGED_NO_UPDATE


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


# --- branch and PR read from prose (temporary, until GitHub is linked) -----
@pytest.mark.parametrize("body,expected", [
    (REAL, "main"),                                    # "Latest branch: main GitHub: ..."
    ("branch: feature/HIR-91-pricing", "feature/HIR-91-pricing"),
    ("Branch - release/1.2", "release/1.2"),
    ("branch name: dev", "dev"),
])
def test_branch_is_read_from_a_labelled_comment(body, expected):
    got = _lines(_FakeLook(comments=[_comment(body)]))
    assert got["branch"] == f"{expected} {ds.FROM_COMMENTS}"


def test_the_branch_stops_at_the_next_label_not_at_the_end_of_the_line():
    """The real comment continues 'main GitHub: agb-admin:' after the branch."""
    assert _lines(_FakeLook(comments=[_comment(REAL)]))["branch"].startswith("main ")


@pytest.mark.parametrize("body", [
    "branch: N/A", "branch: none", "Latest branch: TBD", "branch: -",
])
def test_placeholder_words_are_not_reported_as_a_branch(body):
    assert _lines(_FakeLook(comments=[_comment(body)]))["branch"] == ds.NOT_LINKED


def test_no_branch_label_means_no_branch():
    got = _lines(_FakeLook(comments=[_comment("Worked on the pricing screen today.")]))
    assert got["branch"] == ds.NOT_LINKED


@pytest.mark.parametrize("body,expected", [
    ("PR: #42", "#42"),
    ("Pull request - https://github.com/o/r/pull/7", "https://github.com/o/r/pull/7"),
    ("PR link: https://short.link/pr", "https://short.link/pr"),
])
def test_pr_is_read_from_a_labelled_comment(body, expected):
    got = _lines(_FakeLook(comments=[_comment(body)]))
    assert got["PR"] == f"{expected} {ds.FROM_COMMENTS}"


@pytest.mark.parametrize("body", ["PR: raised", "PR: pending", "PR - none"])
def test_placeholder_words_are_not_reported_as_a_pr(body):
    assert _lines(_FakeLook(comments=[_comment(body)]))["PR"] == ds.NOT_LINKED


def test_the_dev_panel_still_wins_for_every_field():
    look = _FakeLook(
        dev={"branches": ["real/branch"], "commits": ["abc1234"],
             "pull_requests": [{"url": "https://real/pr/1", "name": "", "id": "1",
                                "status": "MERGED"}]},
        comments=[_comment(REAL + " PR: #99 branch: wrong/branch")])
    got = _lines(look)
    assert got["branch"] == "real/branch"          # no "(from comments)" anywhere
    assert got["commit"] == "abc1234"
    assert got["PR"] == "https://real/pr/1"
    assert got["PR merged"] == "Yes"


def test_a_partially_linked_issue_keeps_its_real_data():
    """Only the empty fields fall back to prose."""
    look = _FakeLook(dev={"branches": ["real/branch"], "commits": [], "pull_requests": []},
                     comments=[_comment(REAL)])
    got = _lines(look)
    assert got["branch"] == "real/branch"                       # panel
    assert got["commit"].endswith(ds.FROM_COMMENTS)             # prose


# --- the blank-template comment that broke the first attempt --------------
# A real BHA-117 comment, posted with every field left empty.
BLANK_TEMPLATE = "branch: commit: PR: not raised yet PR merged: no PR exists"


def test_an_empty_template_reports_nothing_rather_than_the_next_label():
    got = _lines(_FakeLook(comments=[_comment(BLANK_TEMPLATE)]))
    assert got["branch"] == ds.NOT_LINKED      # not "commit"
    assert got["PR"] == ds.NOT_LINKED          # not "not"
    assert got["commit"] == ds.NOT_LINKED


@pytest.mark.parametrize("body", [
    "branch: commit: abc1234",                 # label immediately after label
    "branch: PR: #12",
])
def test_a_label_is_never_taken_as_a_branch_name(body):
    assert _lines(_FakeLook(comments=[_comment(body)]))["branch"] == ds.NOT_LINKED


@pytest.mark.parametrize("body", ["PR: not raised yet", "PR: raised", "PR - soon"])
def test_a_pr_needs_a_url_or_a_number(body):
    assert _lines(_FakeLook(comments=[_comment(body)]))["PR"] == ds.NOT_LINKED


def test_the_same_branch_written_two_ways_is_listed_once():
    """"Latest branch: main" and "Live Branch Name : Main" are one branch."""
    body = "Latest branch: main ... Live Branch Name : Main"
    assert _lines(_FakeLook(comments=[_comment(body)]))["branch"] == \
        f"main {ds.FROM_COMMENTS}"


def test_genuinely_different_branches_are_both_listed():
    body = "branch: feature/a and branch: feature/b"
    got = _lines(_FakeLook(comments=[_comment(body)]))["branch"]
    assert got == f"feature/a, feature/b {ds.FROM_COMMENTS}"


# --- PR merged: the developer's own word, or "No update" ------------------
@pytest.mark.parametrize("body,expected", [
    ("PR merged: yes", "Yes"),
    ("PR merged - Y", "Yes"),
    ("Pull request merged: merged on the 12th", "Yes"),
    ("PR merged: no", "No"),
    ("PR merged: no PR exists", "No"),          # the real BHA-117 wording
    ("PR merged - not yet", "No"),
    ("PR merged: pending", "No"),
])
def test_merge_state_is_read_from_the_comment(body, expected):
    assert _lines(_FakeLook(comments=[_comment(body)]))["PR merged"] == expected


@pytest.mark.parametrize("body", [
    "Worked on the pricing screen today.",       # says nothing about a PR
    "PR merged:",                                # the label with no value
    "PR merged: maybe",                          # not classifiable
])
def test_silence_about_merging_reads_as_no_update(body):
    assert _lines(_FakeLook(comments=[_comment(body)]))["PR merged"] == ds.MERGED_NO_UPDATE


def test_no_comments_at_all_reads_as_no_update():
    assert _lines(_FakeLook(comments=[]))["PR merged"] == ds.MERGED_NO_UPDATE


def test_the_newest_comment_wins():
    """issue_comments returns newest-first, so the latest word is the answer."""
    look = _FakeLook(comments=[_comment("PR merged: yes"), _comment("PR merged: no")])
    assert _lines(look)["PR merged"] == "Yes"


def test_a_merged_pr_in_the_dev_panel_still_wins():
    look = _FakeLook(
        dev={"branches": [], "commits": [],
             "pull_requests": [{"url": "https://real/pr/1", "name": "", "id": "1",
                                "status": "MERGED"}]},
        comments=[_comment("PR merged: no")])
    assert _lines(look)["PR merged"] == "Yes"


def test_an_unmerged_panel_pr_with_no_comment_reads_no():
    look = _FakeLook(
        dev={"branches": [], "commits": [],
             "pull_requests": [{"url": "https://real/pr/1", "name": "", "id": "1",
                                "status": "OPEN"}]},
        comments=[])
    assert _lines(look)["PR merged"] == "No"


# --- Task status line -----------------------------------------------------
class _StatusLook(_FakeLook):
    """Returns a different status per key."""

    def __init__(self, statuses):
        super().__init__()
        self.statuses = statuses

    def detail(self, key):
        if key not in self.statuses:
            return None
        return {**self._detail, "key": key, "status_name": self.statuses[key]}


def test_status_entry_uses_the_live_jira_status_verbatim():
    look = _StatusLook({"HIR-91": "In Progress", "HIR-92": "Review", "HIR-90": "Done"})
    assert ds.status_entry("HIR-91", look) == "HIR-91-In Progress"
    assert ds.status_entry("HIR-92", look) == "HIR-92-Review"   # not normalised to "In Review"
    assert ds.status_entry("HIR-90", look) == "HIR-90-Done"


def test_an_unreadable_issue_reports_unknown_rather_than_crashing():
    assert ds.status_entry("HIR-99", _StatusLook({})) == f"HIR-99-{ds.UNKNOWN}"


def test_the_status_line_lists_every_picked_task():
    row = _row("Raghul", ds.CHECKIN_DONE, [], ["HIR-91", "HIR-92", "HIR-90"], [])
    row["task_status"] = ["HIR-91-In Progress", "HIR-92-Review", "HIR-90-Done"]
    block = ds.developer_block(row, jira_configured=True)
    assert "Task status: HIR-91-In Progress, HIR-92-Review, HIR-90-Done" in block


def test_a_developer_with_no_tasks_shows_no_update_for_status():
    row = _row("Soma", ds.CHECKIN_DONE, [], [], [])
    assert f"Task status: {ds.NO_UPDATE}" in ds.developer_block(row, jira_configured=True)


def test_the_status_line_is_split_per_channel_with_the_tasks():
    """A mixed-prefix developer must not leak another project's statuses."""
    row = _row("Raghul", ds.CHECKIN_DONE, [], ["HIR-91", "WS-228"], [])
    row["task_status"] = ["HIR-91-In Progress", "WS-228-Review"]
    routed = ds.route_blocks([row], _cfg(), TODAY, jira_configured=True)
    hiro = "\n".join(routed["Chiro"])
    stacx = "\n".join(routed["Cstacx"])
    assert "HIR-91-In Progress" in hiro and "WS-228" not in hiro
    assert "WS-228-Review" in stacx and "HIR-91" not in stacx


# --- oversized developer blocks -------------------------------------------
def _bulk_row(name: str, n: int, prefix: str = "HIR") -> dict:
    picked = [f"{prefix}-{i}" for i in range(100, 100 + n)]
    return {
        "name": name, "checkin": ds.CHECKIN_DONE, "previous": [], "picked": picked,
        "done": [], "task_status": [f"{k}-In Progress" for k in picked],
        "jira": [[f"{k}: a reasonably long issue summary line",
                  "description: yes",
                  "branch: feat/some-longish-branch-name (from comments)",
                  "commit: b4eda6544adcba92984f2c101ee6fe6db81793b5 (from comments)",
                  "PR: https://github.com/org/repo/pull/1234 (from comments)",
                  "PR merged: No update"] for k in picked],
        "comments": [f"{k}: yes" for k in picked],
    }


def test_a_small_row_is_not_split():
    assert len(ds.split_row(_bulk_row("Raghul", 3), True)) == 1


def test_a_large_row_is_split_into_parts_that_each_fit():
    parts = ds.split_row(_bulk_row("Whale", 40), True)
    assert len(parts) > 1
    for part in parts:
        assert len(ds.developer_block(part, True)) <= ds.SLACK_CHUNK_CHARS


def test_splitting_keeps_every_task_exactly_once_and_in_order():
    original = _bulk_row("Whale", 40)
    parts = ds.split_row(original, True)
    assert [k for p in parts for k in p["picked"]] == original["picked"]
    assert [s for p in parts for s in p["task_status"]] == original["task_status"]
    assert [c for p in parts for c in p["comments"]] == original["comments"]


def test_a_tasks_lines_are_never_split_across_parts():
    """Each part's jira blocks stay aligned with its own picked ids."""
    for part in ds.split_row(_bulk_row("Whale", 40), True):
        assert len(part["jira"]) == len(part["picked"])
        for key, block in zip(part["picked"], part["jira"]):
            assert block[0].startswith(f"{key}:")


def test_continuation_parts_are_labelled_and_do_not_repeat_the_day_summary():
    original = _bulk_row("Whale", 40)
    original["previous"] = ["HIR-1"]
    original["done"] = ["HIR-2"]
    parts = ds.split_row(original, True)
    assert parts[0]["name"] == "Whale" and parts[0]["done"] == ["HIR-2"]
    for part in parts[1:]:
        assert part["name"] == "Whale (continued)"
        assert part["previous"] == [] and part["done"] == []


def test_the_whole_digest_now_respects_the_limit():
    rows = [_bulk_row(f"Dev{i}", 12) for i in range(5)]
    msgs = ds.chunk(ds.compose_blocks(rows, TODAY, True))
    assert max(len(m) for m in msgs) <= ds.SLACK_CHUNK_CHARS


def test_a_single_task_too_large_to_split_is_still_emitted():
    row = _bulk_row("Whale", 1)
    row["jira"] = [["HIR-100: x", "description: " + "y" * (ds.SLACK_CHUNK_CHARS * 2)]]
    parts = ds.split_row(row, True)
    assert len(parts) == 1 and parts[0]["picked"] == ["HIR-100"]


def test_splitting_survives_a_row_with_no_jira_data():
    row = {"name": "Soma", "checkin": ds.CHECKIN_DONE, "previous": [], "picked": [],
           "done": [], "task_status": [], "jira": [], "comments": []}
    assert ds.split_row(row, False) == [row]


# --- per-developer routing ------------------------------------------------
def _cfg_dev() -> SlackConfig:
    return SlackConfig(
        bot_token="x", channel_id=DEFAULT,
        channel_routes={"SP": "Cstacx", "WS": "Cstacx", "HIR": "Chiro", "BHA": "Cbha"},
        developer_routes=_parse_routes("Sahil:Cstacx,Mallesh:Cstacx,Gokul:Chiro"),
    )


def test_a_routed_developer_posts_to_their_own_channel():
    row = _row("Gokul", ds.CHECKIN_DONE, [], ["HIR-1"], [])
    routed = ds.route_blocks([row], _cfg_dev(), TODAY, True)
    assert set(routed) == {"Chiro"}


def test_a_routed_developer_never_falls_back_to_check_in():
    """The point of the change: no tasks must not mean the check-in channel."""
    row = _row("Mallesh", ds.CHECKIN_NONE, [], [], [])
    routed = ds.route_blocks([row], _cfg_dev(), TODAY, True)
    assert set(routed) == {"Cstacx"}
    assert DEFAULT not in routed


def test_a_routed_developers_whole_update_stays_together():
    """Cross-project tasks are not split away to their prefix's channel."""
    row = _row("Gokul", ds.CHECKIN_DONE, [], ["HIR-1", "BHA-2", "SP-3"], [])
    routed = ds.route_blocks([row], _cfg_dev(), TODAY, True)
    assert set(routed) == {"Chiro"}
    text = "\n".join(routed["Chiro"])
    for key in ("HIR-1", "BHA-2", "SP-3"):
        assert key in text


def test_an_unrouted_developer_still_splits_by_prefix():
    row = _row("Raghul", ds.CHECKIN_DONE, [], ["HIR-1", "SP-3"], [])
    routed = ds.route_blocks([row], _cfg_dev(), TODAY, True)
    assert set(routed) == {"Chiro", "Cstacx"}


def test_an_unrouted_taskless_developer_still_falls_back():
    routed = ds.route_blocks([_row("GN", ds.CHECKIN_DONE, [], [], [])],
                             _cfg_dev(), TODAY, True)
    assert set(routed) == {DEFAULT}


def test_developer_routing_is_case_insensitive():
    cfg = _cfg_dev()
    assert cfg.channel_for_developer("sahil") == "Cstacx"
    assert cfg.channel_for_developer("SAHIL") == "Cstacx"
    assert cfg.channel_for_developer("Raghul") is None
    assert cfg.channel_for_developer("") is None


def test_no_developer_routes_configured_changes_nothing():
    row = _row("Sahil", ds.CHECKIN_DONE, [], ["WS-1"], [])
    routed = ds.route_blocks([row], _cfg(), TODAY, True)   # prefix routing only
    assert set(routed) == {"Cstacx"}


def test_a_routed_developer_with_many_tasks_is_still_split_into_messages():
    row = _bulk_row("Sahil", 40, prefix="WS")
    routed = ds.route_blocks([row], _cfg_dev(), TODAY, True)
    assert set(routed) == {"Cstacx"}
    assert max(len(m) for m in ds.chunk(routed["Cstacx"])) <= ds.SLACK_CHUNK_CHARS
