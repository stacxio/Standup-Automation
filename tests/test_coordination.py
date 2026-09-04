"""The coordination bonus (SCORING.md §4.7).

Two named developers who both post in their shared channel on the scoring date
each get +10 on top of the 100 — a day that scored 100 becomes 110. One person
posting into an empty channel is not a conversation, so both sides are needed
and both earn it or neither does.
"""

from __future__ import annotations

import datetime as dt
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from standup_summarizer import scorecard as sc  # noqa: E402
from standup_summarizer.config import ScoreConfig, _parse_coordination  # noqa: E402

TODAY = dt.date(2026, 8, 26)
W, T = sc.Weights(), sc.Thresholds()


def day(**kw) -> sc.DayFacts:
    base = dict(developer="Kavin", date=TODAY, attendance="Present", checked_in=True,
                picked_tasks=(), tasks=())
    base.update(kw)
    return sc.DayFacts(**base)


def score(facts, weights=W) -> dict:
    return sc.score_day(facts, weights, T, computed_at=dt.datetime(2026, 8, 26, 18, 0))


# --- the bonus itself -----------------------------------------------------
def test_coordinating_adds_ten_on_top():
    alone = score(day())
    together = score(day(coordinated=True, coordination_partner="Madhan"))
    assert together["total"] == alone["total"] + 10.0
    assert together["points"]["coordination"] == 10.0


def test_a_hundred_becomes_a_hundred_and_ten():
    """The example that defined the rule: 100 + coordination = 110."""
    task = sc.TaskFacts(key="BHA-130", description="x" * 60, status="Done",
                        status_category="Done", issue_type="Story",
                        has_linked_commit=True, assignee="Kavin",
                        assigned_to_developer=True,
                        comments=(sc.CommentFacts(body="Shipped the S3 migration today.",
                                                  created=TODAY, authored_by_developer=True),))
    perfect = day(picked_tasks=("BHA-130",), tasks=(task,))
    assert score(perfect)["total"] == 100.0
    assert score(replace(perfect, coordinated=True))["total"] == 110.0


def test_not_coordinating_costs_nothing():
    assert score(day())["points"]["coordination"] == 0.0
    assert "coordinated" not in score(day())["flags"]


def test_the_day_is_flagged_so_the_bonus_is_never_silent():
    assert "coordinated" in score(day(coordinated=True))["flags"]


def test_the_bonus_is_not_taken_out_of_the_hundred():
    """Working alone must still be scored out of 100, not 90."""
    W.validate()                       # the six still sum to 100
    assert W.process_max == 40.0       # coordination is not one of them
    assert W.coordination == 10.0


def test_the_size_of_the_bonus_is_configurable():
    generous = replace(W, coordination=25.0)
    assert score(day(coordinated=True), generous)["points"]["coordination"] == 25.0


# --- days there is nothing to add to --------------------------------------
def test_an_absent_day_stays_a_real_zero():
    """0 + 10 for a day nobody worked would be absurd."""
    record = score(day(attendance="Absent", checked_in=False, coordinated=True))
    assert record["total"] == 0.0 and record["points"]["coordination"] == 0.0


def test_a_not_scored_day_gains_nothing():
    record = score(day(data_ok=False, data_error="jira unreachable", coordinated=True))
    assert record["status"] == sc.NOT_SCORED
    assert record["points"]["coordination"] == 0.0


def test_unplanned_work_gets_the_bonus_after_the_rescale_not_inside_it():
    """Folded into the rescale, 10 points would be multiplied by 100/40."""
    incident = sc.TaskFacts(key="SP-99", description="x" * 60, status="In Progress",
                            status_category="In Progress", issue_type="Incident",
                            assigned_to_developer=True)
    facts = day(picked_tasks=("SP-99",), tasks=(incident,))
    assert score(replace(facts, coordinated=True))["total"] == score(facts)["total"] + 10.0


# --- the sheet column and the DM ------------------------------------------
def test_the_column_sits_directly_after_delivery():
    assert sc.DAILY_HEADERS.index("Coordination") == sc.DAILY_HEADERS.index("Delivery") + 1


def test_the_row_carries_the_bonus_in_that_column():
    record = score(day(coordinated=True, coordination_partner="Madhan"))
    row = sc.daily_row(record)
    assert len(row) == len(sc.DAILY_HEADERS)
    assert row[sc.DAILY_HEADERS.index("Coordination")] == "10.0"


def test_the_dm_names_the_partner():
    dm = sc.compose_slack_dm(score(day(coordinated=True, coordination_partner="Madhan")))
    assert "Coordination: +10.0 with Madhan" in dm


def test_the_dm_says_nothing_on_a_day_it_was_not_earned():
    """A 0/10 under the six every day somebody worked alone would just nag."""
    assert "Coordination" not in sc.compose_slack_dm(score(day()))


# --- pairing config -------------------------------------------------------
def test_the_three_pairs_parse():
    pairs = _parse_coordination(
        "C0BEXR128DQ:Kavin+Madhan,C0BEZNFNJ1X:Raghul+Gokul,C0B2FVBQPM5:Sahil+Mallesh")
    assert pairs == (("C0BEXR128DQ", ("Kavin", "Madhan")),
                     ("C0BEZNFNJ1X", ("Raghul", "Gokul")),
                     ("C0B2FVBQPM5", ("Sahil", "Mallesh")))


@pytest.mark.parametrize("raw", ["", None, "junk", "C1:OnlyOne", "C1:A+B+C", ":A+B", "C1:"])
def test_a_malformed_pair_is_skipped_not_raised(raw):
    """A typo in extra credit must not fail a run that scores real work."""
    assert _parse_coordination(raw) == ()


def test_lookup_finds_either_side_of_a_pair():
    cfg = ScoreConfig(weights=W, thresholds=T,
                      coordination_pairs=(("C0BEXR128DQ", ("Kavin", "Madhan")),))
    assert cfg.coordination_for("Kavin") == ("C0BEXR128DQ", "Madhan")
    assert cfg.coordination_for("Madhan") == ("C0BEXR128DQ", "Kavin")
    assert cfg.coordination_for("Soma") is None


def test_a_pair_written_under_a_retired_name_still_matches(monkeypatch):
    """SCORE_COORDINATION names the pair by roster label, so a rename reaches it.

    Everything the bonus compares against is canonical, so a pair left saying
    "Kavin" after the roster moved to "Kevin" matches nobody: `coordination_for`
    returns None and both halves quietly lose 10 points a day. Renaming somebody
    must not cost them points somewhere else in the config.
    """
    monkeypatch.setenv("ROSTER_ALIASES", "GN:Kevin,Kavin:Kevin")
    import build_attendance as ba
    import build_scorecard as bs

    stale = ScoreConfig(weights=W, thresholds=T,
                        coordination_pairs=(("C0BEXR128DQ", ("Kavin", "Madhan")),))
    fresh = bs.canonical_pairs(stale, ba.roster_name_map(["Kevin", "Madhan"]))
    assert fresh.coordination_pairs == (("C0BEXR128DQ", ("Kevin", "Madhan")),)
    assert fresh.coordination_for("Kevin") == ("C0BEXR128DQ", "Madhan")


# --------------------------------------------------------------------------
# The daily channel post — the table people actually read
# --------------------------------------------------------------------------
def _post(*records) -> str:
    return sc.compose_slack_roster(list(records))


def test_the_daily_post_has_the_column_right_after_delivery():
    """It was in the sheet and the DM but not here, which is where people look."""
    post = _post(score(day(coordinated=True, coordination_partner="Madhan")))
    header = next(line for line in post.splitlines() if line.startswith("Developer"))
    assert header.index("Coordination") > header.index("Delivery")
    assert header.index("Coordination") < header.index("State")


def test_the_post_signs_the_bonus():
    """"+10.0" reads as added on; "10.0" would read as ten out of some hundred."""
    assert "+10.0" in _post(score(day(coordinated=True)))


def test_the_post_dashes_a_day_it_was_not_earned():
    post = _post(score(day()))
    row = next(line for line in post.splitlines() if line.startswith("Kavin"))
    assert "+" not in row


def test_the_post_explains_the_column():
    assert "Coordination +10" in _post(score(day(coordinated=True)))


def test_the_bonus_is_named_in_the_persons_breakdown_too():
    """The column says a bonus was earned; the breakdown says who with."""
    block = _post(score(day(coordinated=True,
                            coordination_partner="Madhan"))).split("```")[2]
    assert "• Coordination: +10.0 with Madhan" in block
    assert "• Coordination" not in _post(score(day())).split("```")[2]


def test_an_unscored_row_keeps_the_columns_aligned():
    unscored = score(day(data_ok=False, data_error="jira unreachable"))
    post = _post(score(day(coordinated=True)), unscored)
    # The table only — the per-developer breakdown under it is prose, not columns.
    body = [l for l in post.split("```")[1].splitlines() if l]
    assert len({len(l.split()) for l in body}) <= 3   # header + scored + not-scored shapes
    assert "Not scored" in post
