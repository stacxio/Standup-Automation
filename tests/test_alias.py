"""Roster aliases: one person, one row, across a change of Slack account.

Kavin G N posted from "G N" (U0AMMMQC4Q2) until 24-08-2026 and from "Kavin"
(U0BS24Y9MLK) after it. The roll-call kept saying GN, so the check-ins arrived
under a name the roster did not have: GN was marked present and scored 10/100
for "no tasks" on 25-08, while Kavin — who had named BHA-130 that morning — was
on no roster at all. ROSTER_ALIASES="GN:Kavin" folds both onto one label.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import build_attendance as ba  # noqa: E402


@pytest.fixture
def aliased(monkeypatch):
    monkeypatch.setenv("ROSTER_ALIASES", "GN:Kavin")
    return ba._alias_map()


def test_no_aliases_configured_is_an_empty_map(monkeypatch):
    monkeypatch.delenv("ROSTER_ALIASES", raising=False)
    assert ba._alias_map() == {}


def test_both_names_resolve_to_the_new_one(aliased):
    assert ba._canonical("GN", aliased) == "Kavin"
    assert ba._canonical("G N", aliased) == "Kavin"
    assert ba._canonical("Kavin", aliased) == "Kavin"


def test_an_unaliased_name_is_left_alone(aliased):
    assert ba._canonical("Soma", aliased) == "Soma"


@pytest.mark.parametrize("raw, expected", [
    ("", {}),
    ("GN:Kavin", {"gn": "Kavin", "kavin": "Kavin"}),
    ("  GN : Kavin  ", {"gn": "Kavin", "kavin": "Kavin"}),
    ("GN:Kavin,Old Name:New", {"gn": "Kavin", "kavin": "Kavin",
                               "oldname": "New", "new": "New"}),
    ("GN", {}),          # no separator — not a pair
    ("GN:", {}),         # half a pair is not a rename
    (":Kavin", {}),
])
def test_alias_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("ROSTER_ALIASES", raw)
    assert ba._alias_map() == expected


def test_the_retired_name_still_resolves_after_the_roll_call_moves_on(monkeypatch):
    """The roll-call says "Kavin" now; a year of "G N" history must not strand."""
    monkeypatch.setenv("ROSTER_ALIASES", "GN:Kavin")
    mapping = ba.roster_name_map(["Kavin", "Soma"])
    assert ba._short_of("G N", mapping) == "Kavin"
    assert ba._short_of("Kavin", mapping) == "Kavin"
    assert ba._short_of("Soma Pani", mapping) == "Soma"


def test_the_new_name_resolves_before_the_roll_call_starts_using_it(monkeypatch):
    """The roll-call still says "GN"; check-ins already arrive as "Kavin"."""
    monkeypatch.setenv("ROSTER_ALIASES", "GN:Kavin")
    mapping = ba.roster_name_map(["GN", "Soma"])
    assert ba._short_of("Kavin", mapping) == "Kavin"
    assert ba._short_of("G N", mapping) == "Kavin"


def test_roster_name_map_without_aliases_is_just_the_roster(monkeypatch):
    monkeypatch.delenv("ROSTER_ALIASES", raising=False)
    assert ba.roster_name_map(["Soma", "Raghul"]) == {"soma": "Soma", "raghul": "Raghul"}


def test_master_seed_carries_the_current_name():
    """The Master tab seeds employee details; a stale label misfiles payslips."""
    names = [row[0] for row in ba.MASTER_SEED]
    assert "Kavin" in names and "GN" not in names


# --------------------------------------------------------------------------
# Which of two accounts gets the DM
# --------------------------------------------------------------------------
def _pick(members, name_map):
    """The tie-break slack_user_ids applies, without the Slack round trips."""
    ids, exact = {}, {}
    for uid, real_name in members:
        short = ba._short_of(real_name, name_map)
        if ba._fold(real_name) == ba._fold(short):
            exact.setdefault(short, uid)
        ids.setdefault(short, uid)
    ids.update(exact)
    return ids


def test_the_dm_goes_to_the_account_that_owns_the_roster_name(monkeypatch):
    """Membership order put the retired account first and it won the DM."""
    monkeypatch.setenv("ROSTER_ALIASES", "GN:Kavin")
    mapping = ba.roster_name_map(["Kavin", "Soma"])
    members = [("U0AMMMQC4Q2", "KAVIN G N"),      # retired, listed first
               ("U0BS24Y9MLK", "Kavin"),
               ("U08C4R9CCJU", "Soma Pani")]
    assert _pick(members, mapping)["Kavin"] == "U0BS24Y9MLK"


def test_the_order_of_the_two_accounts_does_not_matter(monkeypatch):
    monkeypatch.setenv("ROSTER_ALIASES", "GN:Kavin")
    mapping = ba.roster_name_map(["Kavin"])
    forward = [("U0BS24Y9MLK", "Kavin"), ("U0AMMMQC4Q2", "KAVIN G N")]
    assert _pick(forward, mapping)["Kavin"] == "U0BS24Y9MLK"


def test_one_account_still_resolves_without_an_exact_match(monkeypatch):
    """"Malleshwaran M" never equals "Mallesh"; first-seen must still win."""
    monkeypatch.delenv("ROSTER_ALIASES", raising=False)
    mapping = ba.roster_name_map(["Mallesh"])
    assert _pick([("U0BEK6E250V", "Malleshwaran M")], mapping)["Mallesh"] == "U0BEK6E250V"
