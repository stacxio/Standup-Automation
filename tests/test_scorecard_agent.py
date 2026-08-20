"""Scorecard agent glue: attendance reading, Jira assembly, history, sheet upserts."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import build_scorecard as bs  # noqa: E402
from standup_summarizer import scorecard as sc  # noqa: E402
from standup_summarizer.config import JiraConfig  # noqa: E402

TODAY = dt.date(2026, 8, 12)
NAMES = {"raghul": "Raghul", "soma": "Soma"}
JIRA = JiraConfig(base_url="https://jira.example.com", email="e@x", api_token="t")


# --- attendance (SCORING.md §7) -------------------------------------------
def test_attendance_reads_the_roll_call():
    roll = {TODAY: {"Raghul": "Present", "Soma": "Half Day"}}
    assert bs.attendance_of("Raghul", TODAY, roll, {}) == ("Present", True)
    assert bs.attendance_of("Soma", TODAY, roll, {}) == ("Half Day", True)


def test_leave_and_weekend_come_from_the_attendance_agent():
    assert bs.attendance_of("Raghul", TODAY, {}, {TODAY: {"Raghul"}})[0] == "Leave"
    saturday = dt.date(2026, 8, 15)
    assert bs.attendance_of("Raghul", saturday, {}, {})[0] == "Weekend"


def test_a_missing_roll_call_is_a_data_failure_not_an_absence():
    # No roll-call at all for the day.
    facts = bs.build_day_facts("Raghul", TODAY, [], {}, {}, bs.JiraFacts(None, NAMES), None)
    assert facts.data_ok is False and "no roll-call" in facts.data_error


def test_being_left_off_an_existing_roll_call_is_also_not_an_absence():
    roll = {TODAY: {"Soma": "Present"}}          # Raghul omitted
    facts = bs.build_day_facts("Raghul", TODAY, [], roll, {}, bs.JiraFacts(None, NAMES), None)
    assert facts.data_ok is False
    assert facts.data_error == "not listed in the day's roll-call"


def test_an_explicit_absence_is_scored_as_a_real_zero():
    roll = {TODAY: {"Raghul": "Absent"}}
    facts = bs.build_day_facts("Raghul", TODAY, [], roll, {}, bs.JiraFacts(None, NAMES), None)
    assert facts.data_ok is True and facts.attendance == "Absent"
    record = sc.score_day(facts, sc.Weights(), sc.Thresholds(),
                          computed_at=dt.datetime(2026, 8, 12, 18, 0))
    assert record["status"] == sc.SCORED and record["total"] == 0.0


def test_jira_unconfigured_only_blocks_days_that_committed_tasks():
    roll = {TODAY: {"Raghul": "Present", "Soma": "Present"}}
    none = bs.JiraFacts(None, NAMES)
    with_tasks = bs.build_day_facts("Raghul", TODAY, ["SP-12"], roll, {}, none, None)
    without = bs.build_day_facts("Soma", TODAY, [], roll, {}, none, None)
    assert with_tasks.data_ok is False and with_tasks.data_error == "jira not configured"
    assert without.data_ok is True


# --- Jira fact assembly ---------------------------------------------------
class FakeJira(bs.JiraFacts):
    """JiraFacts with the network replaced by a dict of canned issues."""

    def __init__(self, issues: dict, names=NAMES):
        super().__init__(JIRA, names)
        self.issues = issues
        self.calls = 0

    def _fetch(self, key):
        self.calls += 1
        return self.issues.get(key)


def issue(key="SP-12", *, desc="x" * 60, status="In Progress", category="indeterminate",
          kind="Story", linked=False, comments=()):
    return {
        "detail": {"id": "1001", "key": key, "summary": "s", "description": desc,
                   "status_name": status, "status_category": category, "issue_type": kind},
        "comments": list(comments),
        "has_linked_commit": linked,
    }


def comment(author="Raghul", body="Finished the validation layer today.",
            created="2026-08-12T10:15:30.123+0530"):
    return {"author": author, "created": created, "body": body}


def test_task_facts_map_jira_fields_onto_the_scorer():
    facts = FakeJira({"SP-12": issue(status="Done", category="done", kind="Bug",
                                     linked=True, comments=[comment()])})
    task = facts.task("SP-12", "Raghul")
    assert task.status_category == "Done" and task.status == "Done"
    assert task.issue_type == "Bug" and task.has_linked_commit is True
    assert task.url == "https://jira.example.com/browse/SP-12"
    assert task.comments[0].created == TODAY
    assert task.comments[0].authored_by_developer is True


@pytest.mark.parametrize("category,expected", [
    ("done", "Done"), ("indeterminate", "In Progress"), ("new", "To Do"),
])
def test_status_categories_are_translated(category, expected):
    facts = FakeJira({"SP-12": issue(category=category)})
    assert facts.task("SP-12", "Raghul").status_category == expected


def test_comment_authorship_uses_loose_roster_matching():
    facts = FakeJira({"SP-12": issue(comments=[comment(author="Raghul Kumar"),
                                               comment(author="Soma Pani")])})
    task = facts.task("SP-12", "Raghul")
    assert [c.authored_by_developer for c in task.comments] == [True, False]


def test_unparseable_comment_dates_are_dropped_not_guessed():
    facts = FakeJira({"SP-12": issue(comments=[comment(created="yesterday"), comment()])})
    assert len(facts.task("SP-12", "Raghul").comments) == 1


def test_each_key_is_fetched_once_however_many_developers_named_it():
    facts = FakeJira({"SP-12": issue()})
    facts.task("SP-12", "Raghul")
    facts.task("SP-12", "Soma")
    assert facts.calls == 1


def test_an_unreadable_issue_is_recorded_and_forces_not_scored():
    facts = FakeJira({"SP-12": issue()})          # SP-15 is missing
    roll = {TODAY: {"Raghul": "Present"}}
    day = bs.build_day_facts("Raghul", TODAY, ["SP-12", "SP-15"], roll, {}, facts, None)
    assert "SP-15" in facts.failed
    record = sc.score_day(day, sc.Weights(), sc.Thresholds(),
                          computed_at=dt.datetime(2026, 8, 12, 18, 0))
    assert record["status"] == sc.NOT_SCORED and "SP-15" in record["reason"]


# --- trailing median (§5.1) -----------------------------------------------
def history_row(date: str, developer: str, picked: int, status=sc.SCORED) -> list[str]:
    row = [""] * len(sc.DAILY_HEADERS)
    row[sc.DATE_COLUMN] = date
    row[sc.DEVELOPER_COLUMN] = developer
    row[sc.DAILY_HEADERS.index("Status")] = status
    row[sc.DAILY_HEADERS.index("Tasks Picked")] = str(picked)
    return row


def days_of(counts: list[int], developer="Raghul", status=sc.SCORED) -> list[list[str]]:
    return [history_row((TODAY - dt.timedelta(days=i + 1)).isoformat(), developer, c, status)
            for i, c in enumerate(counts)]


def test_median_needs_enough_history_to_be_fair():
    assert bs.median_picked(days_of([3] * 9), "Raghul", TODAY) is None
    assert bs.median_picked(days_of([3] * 10), "Raghul", TODAY) == 3.0


def test_median_ignores_other_developers_and_unscored_days():
    rows = days_of([3] * 10) + days_of([99] * 10, developer="Soma")
    rows += days_of([99] * 5, status=sc.NOT_SCORED)
    assert bs.median_picked(rows, "Raghul", TODAY) == 3.0


def test_median_excludes_the_day_being_scored_and_anything_older_than_the_window():
    rows = days_of([2] * 10) + [history_row(TODAY.isoformat(), "Raghul", 99)]
    rows += [history_row("2026-01-01", "Raghul", 99)]
    assert bs.median_picked(rows, "Raghul", TODAY) == 2.0


def test_median_survives_malformed_rows():
    rows = days_of([4] * 10) + [["junk"], history_row("not-a-date", "Raghul", 1)]
    assert bs.median_picked(rows, "Raghul", TODAY) == 4.0


# --- key extraction (SCORING.md §4.2) -------------------------------------
PREFIXES = frozenset({"SP", "WS", "HIR", "BHA"})


def standup(text: str) -> dict:
    return {(TODAY.isoformat(), "Raghul"): [text]}


def test_only_keys_for_real_projects_are_picked():
    """Real stand-ups produced ST-11, PI-01, ORG-404 — none are Jira issues."""
    text = "Working on HIR-79 and WS-228. Also reviewed ST-11 and the ORG-404 page."
    assert bs.picked_from_slack(standup(text), TODAY, "Raghul", PREFIXES) == ["HIR-79", "WS-228"]


def test_an_unknown_prefix_does_not_cost_the_whole_day():
    """An unresolvable key sends the day to Not Scored, so it must be filtered."""
    picked = bs.picked_from_slack(standup("Picked up TEMP-2 today."), TODAY, "Raghul", PREFIXES)
    assert picked == []


def test_prefix_matching_is_case_insensitive():
    assert bs.picked_from_slack(standup("done hir-79"), TODAY, "Raghul", PREFIXES) == ["HIR-79"]


def test_without_configured_prefixes_nothing_is_filtered():
    picked = bs.picked_from_slack(standup("ST-11 and HIR-79"), TODAY, "Raghul", None)
    assert picked == ["ST-11", "HIR-79"]


def test_prefixes_fall_back_to_the_existing_routing_config():
    from standup_summarizer.config import ScoreConfig

    cfg = ScoreConfig(weights=sc.Weights(), thresholds=sc.Thresholds())
    assert cfg.known_prefixes({"SP": "C1", "hir": "C2"}, {"BHA": "BHA"}) == {"SP", "HIR", "BHA"}


def test_an_explicit_prefix_list_wins_over_the_fallback():
    from standup_summarizer.config import ScoreConfig

    cfg = ScoreConfig(weights=sc.Weights(), thresholds=sc.Thresholds(),
                      project_prefixes=frozenset({"ONLY"}))
    assert cfg.known_prefixes({"SP": "C1"}) == {"ONLY"}


# --- commitment snapshot --------------------------------------------------
def commitment_row(date: str, developer: str, keys: str, at="2026-08-12T11:00:00"):
    return [date, developer, keys, at]


def test_commitments_are_read_back_for_the_right_day():
    rows = [commitment_row("2026-08-12", "Raghul", "SP-12, SP-15"),
            commitment_row("2026-08-12", "Soma", ""),
            commitment_row("2026-08-11", "Raghul", "HIR-72")]
    frozen = bs.commitments_for(rows, TODAY)
    assert frozen == {"Raghul": ["SP-12", "SP-15"], "Soma": []}


def test_merge_commitments_upserts_by_date_and_developer():
    existing = [commitment_row("2026-08-11", "Raghul", "HIR-72")]
    rows = bs.merge_commitments(existing, [
        commitment_row("2026-08-11", "Raghul", "HIR-99"),   # corrects
        commitment_row("2026-08-12", "Raghul", "SP-12"),    # adds
    ])
    assert len(rows) == 2
    assert rows[0][0] == "2026-08-12"                       # newest first
    assert rows[1][2] == "HIR-99"


def test_merge_commitments_drops_blank_rows():
    assert bs.merge_commitments([[], [""]], [commitment_row("2026-08-12", "Raghul", "")]) == [
        commitment_row("2026-08-12", "Raghul", "")]


# --- month matrix round-trip through the fact table -----------------------
def test_records_are_read_back_from_the_sheet_for_the_month():
    scored = sc.daily_row({
        "date": "2026-08-12", "developer": "Raghul", "attendance": "Present",
        "status": sc.SCORED, "reason": "", "picked_tasks": [], "tasks_picked": 0,
        "tasks_done": 0, "points": {}, "process": 10.0, "delivery": 0.0,
        "volume_factor": 1.0, "total": 10.0, "band": "At Risk", "flags": [],
        "evidence": {}, "computed_at": "2026-08-12T18:00:00",
    })
    other_month = list(scored)
    other_month[sc.DATE_COLUMN] = "2026-07-31"

    records = bs._records_from_rows([scored, other_month, ["junk"]], TODAY)
    assert len(records) == 1
    assert records[0] == {"developer": "Raghul", "date": "2026-08-12",
                          "attendance": "Present", "status": sc.SCORED, "total": 10.0}


def test_a_blank_total_reads_back_as_none_not_zero():
    row = sc.daily_row(sc.score_day(
        sc.DayFacts(developer="Raghul", date=TODAY, data_ok=False, data_error="jira: 503"),
        sc.Weights(), sc.Thresholds(), computed_at=dt.datetime(2026, 8, 12, 18, 0)))
    records = bs._records_from_rows([row], TODAY)
    assert records[0]["total"] is None
    assert sc.period_average(records) is None


# --- where the daily scorecard posts --------------------------------------
def _score_cfg(**kw):
    from standup_summarizer.config import ScoreConfig
    return ScoreConfig(weights=sc.Weights(), thresholds=sc.Thresholds(), **kw)


def test_the_scorecard_posts_to_the_check_in_channel_by_default():
    assert _score_cfg().score_channels("C-checkin") == ["C-checkin"]


def test_listed_channels_replace_the_default():
    cfg = _score_cfg(post_channels=("C-checkin", "C-ops"))
    assert cfg.score_channels("C-checkin") == ["C-checkin", "C-ops"]


def test_the_score_can_go_somewhere_other_than_the_team_channel():
    """Listing only the ops channel moves the score off the team channel."""
    assert _score_cfg(post_channels=("C-ops",)).score_channels("C-checkin") == ["C-ops"]


def test_a_channel_listed_twice_is_posted_to_once():
    cfg = _score_cfg(post_channels=("C-ops", "C-ops", "C-checkin"))
    assert cfg.score_channels("C-checkin") == ["C-ops", "C-checkin"]


def test_blank_entries_are_ignored():
    assert _score_cfg(post_channels=("", "C-ops", "")).score_channels("C-x") == ["C-ops"]


def test_failure_alerts_keep_their_own_destination():
    """Adding the score to a channel must not move the data-failure alerts."""
    cfg = _score_cfg(post_channels=("C-ops",), ops_channel_id="C-alerts")
    assert cfg.score_channels("C-checkin") == ["C-ops"]
    assert cfg.ops_channel_id == "C-alerts"


# --- DM delivery ----------------------------------------------------------
class _Spy:
    """Captures post_slack calls instead of sending them."""

    def __init__(self):
        self.sent = []

    def __call__(self, cfg, channel, text, label):
        self.sent.append((channel, label))


def _dm_record(name: str) -> dict:
    return {"developer": name, "date": "2026-08-19", "status": sc.SCORED,
            "attendance": "Present", "total": 50.0, "process": 20.0, "delivery": 30.0,
            "band": "Needs Attention", "points": {}, "tasks_picked": 0, "tasks_done": 0,
            "tasks_credit": 0, "flags": [], "evidence": {}, "reason": ""}


def test_every_developer_with_an_id_is_dmed(monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(bs, "post_slack", spy)
    records = [_dm_record("Raghul"), _dm_record("Soma")]
    missed = bs.dm_developers(None, records, {"Raghul": "U1", "Soma": "U2"})
    assert missed == []
    assert [c for c, _ in spy.sent] == ["U1", "U2"]


def test_a_developer_without_a_slack_id_is_reported_not_swallowed(monkeypatch):
    """Mallesh is not in the check-in channel, so no id resolves for him."""
    spy = _Spy()
    monkeypatch.setattr(bs, "post_slack", spy)
    records = [_dm_record("Raghul"), _dm_record("Mallesh")]
    missed = bs.dm_developers(None, records, {"Raghul": "U1"})
    assert missed == ["Mallesh"]
    assert [c for c, _ in spy.sent] == ["U1"]       # the others still get theirs


def test_nobody_reachable_returns_them_all(monkeypatch):
    monkeypatch.setattr(bs, "post_slack", _Spy())
    assert bs.dm_developers(None, [_dm_record("A"), _dm_record("B")], {}) == ["A", "B"]
