"""Run-date parsing, shared by the orchestrator and the agents it calls."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from standup_summarizer.dates import day_from_argv, parse_day  # noqa: E402

AUG19 = dt.date(2026, 8, 19)


@pytest.mark.parametrize("text", ["19-08-2026", "2026-08-19", "19/08/2026", "2026/08/19"])
def test_both_orders_mean_the_same_day(text):
    """A four-digit year is unambiguous at either end."""
    assert parse_day(text) == AUG19


def test_surrounding_whitespace_is_tolerated():
    assert parse_day("  19-08-2026 ") == AUG19


@pytest.mark.parametrize("text", ["", None, "tomorrow", "19-08", "2026", "32-01-2026",
                                  "19-13-2026", "tests/test_dates.py"])
def test_anything_else_is_refused_with_the_accepted_forms(text):
    with pytest.raises(ValueError) as exc:
        parse_day(text)
    assert "DD-MM-YYYY" in str(exc.value)


@pytest.mark.parametrize("argv", [
    ["x.py", "--date", "19-08-2026"],
    ["x.py", "--date=19-08-2026"],
    ["x.py", "--notify", "--date", "2026-08-19", "--dry-run"],
])
def test_the_date_flag_is_found_among_other_flags(argv):
    """The agents scan argv and tolerate flags they do not recognise."""
    assert day_from_argv(argv) == AUG19


def test_no_date_flag_means_today():
    assert day_from_argv(["x.py", "--notify"]) == dt.date.today()


def test_an_explicit_default_wins_over_today():
    assert day_from_argv(["x.py"], default=AUG19) == AUG19


def test_a_dangling_date_flag_falls_back_rather_than_crashing():
    assert day_from_argv(["x.py", "--date"]) == dt.date.today()


def test_a_bad_date_value_raises_rather_than_silently_using_today():
    with pytest.raises(ValueError):
        day_from_argv(["x.py", "--date", "nonsense"])
