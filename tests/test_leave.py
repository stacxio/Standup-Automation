"""Leave detection (build_attendance): a notice, not the word.

On 25-08-2026 two working developers were scored "Not Scored: Leave". The team
was building a project named "Leave Management System", and three things
compounded:

  * any message with the word "leave" + a date was read as a leave notice, so
    a check-in about that project looked like one;
  * the person was picked by a bare substring match on full names, and "G N"
    strips to "gn" — which occurs inside "design", so a colleague's check-in
    about design pages named GN as the one on leave;
  * an inferred leave outranked the roll-call, which had said Present for all
    seven that day.

The same day surfaced a second gap, covered at the end of this file: the roster
is built from roll-call lines alone, so Kavin — who checked in naming BHA-130 —
was on no roll-call and therefore invisible to every agent, silently.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from build_attendance import (  # noqa: E402
    _is_leave_notice,
    _name_pattern,
    cell_value,
    unrostered_checkins as ba_unrostered,
    warn_unrostered as ba_warn,
)

DAY = dt.date(2026, 8, 25)

# The two real check-ins that were misread, abbreviated.
SOMA_CHECKIN = (
    "Date: 25.08.2026\n"
    "Project: Leave  Management\n"
    "Task IDs: SP-23\n"
    "SP-23 - Implement Leave Management System with Docker Environment Setup\n"
    "What Value Will It Add?\nFor tracking the leaves"
)
SAHIL_CHECKIN = (
    "Date: 25.08.2026\n"
    "1. WS-268 - Implement Leave Management System with Docker Environment Setup: "
    "Guide her through the leave workflow (apply -> validate -> approve/reject -> "
    "balance update -> history), verify role-based login.\n"
    "2. WS-269 - Create Separate Design Pages for Each Industry."
)


@pytest.mark.parametrize(
    "text",
    [
        "GN is on leave on 26-08-2026",
        "I am taking leave 26-08-2026",
        "Applying for leave on 26-08-2026",
        "Sick leave 26-08-2026",
        "Leave request for 26-08-2026",
        "26-08-2026 half day leave",
        "Soma will be on leave 26-08-2026",
    ],
)
def test_declarations_are_notices(text):
    assert _is_leave_notice(text)


@pytest.mark.parametrize(
    "text",
    [
        SOMA_CHECKIN,
        SAHIL_CHECKIN,
        "Date: 25.08.2026 Built the leave balance API and the leave policy screen",
        "Shipped the leave tracker dashboard and the leave report",
    ],
)
def test_project_talk_is_not_a_notice(text):
    """A project *named* Leave Management is not somebody's absence."""
    assert not _is_leave_notice(text)


@pytest.mark.parametrize(
    "text, matches",
    [
        ("create separate design pages", False),   # the original misfire
        ("assignment done", False),
        ("gn is on leave", True),
        ("g n is out today", True),
        ("G.N. out today", True),
    ],
)
def test_short_names_match_on_word_boundaries(text, matches):
    assert bool(_name_pattern("G N").search(text)) is matches


def test_longest_name_wins_over_a_prefix():
    """"Madhan Kumar P" and "Madhan" both match; the specific one is the person."""
    text = "madhan kumar p is on leave 26-08-2026"
    named = [n for n in ("Madhan", "Madhan Kumar P") if _name_pattern(n).search(text)]
    assert max(named, key=len) == "Madhan Kumar P"


@pytest.mark.parametrize(
    "marked, on_leave, expected",
    [
        ("Present", True, "Present"),    # the regression: roll-call wins
        ("Half Day", True, "Half Day"),
        ("Absent", True, "Leave"),       # leave refines an absence
        ("", True, "Leave"),             # and fills a blank
        ("Present", False, "Present"),
        ("", False, ""),
    ],
)
def test_explicit_attendance_outranks_an_inferred_leave(marked, on_leave, expected):
    roll_calls = {DAY: {"GN": marked}} if marked else {}
    leaves = {DAY: {"GN"}} if on_leave else {}
    assert cell_value("GN", DAY, roll_calls, leaves) == expected


def test_weekend_still_wins_over_everything():
    saturday = dt.date(2026, 8, 29)
    assert cell_value("GN", saturday, {saturday: {"GN": "Present"}}, {saturday: {"GN"}}) == "Weekend"


# --------------------------------------------------------------------------
# Roster gap: a check-in from someone no roll-call ever lists
# --------------------------------------------------------------------------
def test_unrostered_checkin_is_reported():
    """Kavin checked in on 24 and 25 Aug 2026 and was scored by nothing."""
    order = ["GN", "Soma", "Raghul"]
    status_dates = {
        dt.date(2026, 8, 24): {"GN", "Kavin"},
        dt.date(2026, 8, 25): {"Soma", "Kavin"},
    }
    assert ba_unrostered(order, status_dates) == {"Kavin": [dt.date(2026, 8, 24), dt.date(2026, 8, 25)]}


def test_a_full_roster_reports_nothing():
    order = ["GN", "Soma"]
    status_dates = {dt.date(2026, 8, 25): {"GN", "Soma"}}
    assert ba_unrostered(order, status_dates) == {}


def test_warning_names_the_person_and_the_days(capsys):
    order = ["GN"]
    status_dates = {dt.date(2026, 8, 25): {"Kavin"}}
    lines = ba_warn(order, status_dates)
    out = capsys.readouterr().out
    assert lines and "Kavin" in out and "2026-08-25" in out
    assert "roll-call" in out


def test_no_warning_when_nothing_is_missing(capsys):
    assert ba_warn(["GN"], {dt.date(2026, 8, 25): {"GN"}}) == []
    assert capsys.readouterr().out == ""
