"""Roll-call line parsing (build_attendance._rollcall_entry).

Attendance comes from lines like "GN-Present(Full Day)", but stand-up posts are
full of "Label: sentence" lines that match the same shape. A substring scan for
"half"/"present" matches prose ("...on behalf of...", "...the presentation..."),
which both invents a team member and makes the parser treat the whole stand-up
as a roll-call — so its author is never credited with a check-in.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from build_attendance import _rollcall_entry  # noqa: E402
from build_report import _ROLLCALL_LINE as REPORT_ROLLCALL  # noqa: E402


@pytest.mark.parametrize(
    "line, expected",
    [
        ("GN-Present(Full Day)", ("GN", "Present")),
        ("Soma-Absent", ("Soma", "Absent")),
        ("Raghul-Half day", ("Raghul", "Half Day")),
        ("Sahil-Present(Full Day", ("Sahil", "Present")),   # unclosed paren, seen live
        ("Soma - Present", ("Soma", "Present")),
        ("GN:Present(Full Day)", ("GN", "Present")),
        ("Sahil Thakur - Present", ("Sahil Thakur", "Present")),
        ("  GN – Absent  ", ("GN", "Absent")),              # en dash
    ],
)
def test_parses_real_rollcall_lines(line, expected):
    assert _rollcall_entry(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        # The live regression: "behalf" contains "half".
        "What value will it add: Faster run monitoring and debugging, and more"
        " natural AI replies — the bot answers on behalf of the team",
        "What is the task: Prepare the presentation deck",   # "present" in a word
        "Notes: chase the absentee list",                    # "absent" in a word
        "Project: Standup Automation",
        "Date 27-07-2026",
        "Task ID: WS-197 — Automations improvements",
        "what value will it add: It will standardize the Process",
    ],
)
def test_standup_prose_is_not_a_rollcall(line):
    assert _rollcall_entry(line) is None


def test_status_token_must_be_short():
    """A name-shaped label followed by prose is not attendance."""
    assert _rollcall_entry("Soma - Present the new dashboard to the client tomorrow") is None


def test_name_must_not_be_a_sentence():
    assert _rollcall_entry("What got moved today: Absent") is None


@pytest.mark.parametrize(
    "line, is_rollcall",
    [
        ("GN-Present(Full Day)", True),
        ("Soma-Absent", True),
        ("Raghul-Half day", True),
        ("What value will it add: the bot answers on behalf of the team", False),
        ("What is the task: Prepare the presentation deck", False),
    ],
)
def test_report_excludes_the_same_lines(line, is_rollcall):
    """build_report drops messages containing a roll-call line — same rule."""
    assert bool(REPORT_ROLLCALL.match(line)) is is_rollcall
