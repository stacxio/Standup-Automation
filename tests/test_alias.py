"""Roster aliases: one person, one row, across a change of Slack account.

Kavin G N posted from "G N" (U0AMMMQC4Q2) until 24-08-2026 and from "Kavin"
(U0BS24Y9MLK) after it. The roll-call kept saying GN, so the check-ins arrived
under a name the roster did not have: GN was marked present and scored 10/100
for "no tasks" on 25-08, while Kavin — who had named BHA-130 that morning — was
on no roster at all.

It happened a second time on 27-08-2026. He changed the email on the account,
the roll-call moved to "Kevin", and Slack still said "Kavin" — so the 27-08
summary read "Kevin: No update from developer" over a full day's work on
BHA-135/137/138/139. ROSTER_ALIASES="GN:Kevin,Kavin:Kevin" chains all three
names onto the one label.
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
    monkeypatch.setenv("ROSTER_ALIASES", "GN:Kevin,Kavin:Kevin")
    return ba._alias_map()


def test_no_aliases_configured_is_an_empty_map(monkeypatch):
    monkeypatch.delenv("ROSTER_ALIASES", raising=False)
    assert ba._alias_map() == {}


def test_every_name_in_the_chain_resolves_to_the_current_one(aliased):
    assert ba._canonical("GN", aliased) == "Kevin"
    assert ba._canonical("G N", aliased) == "Kevin"
    assert ba._canonical("Kavin", aliased) == "Kevin"
    assert ba._canonical("Kevin", aliased) == "Kevin"


def test_an_unaliased_name_is_left_alone(aliased):
    assert ba._canonical("Soma", aliased) == "Soma"


@pytest.mark.parametrize("raw, expected", [
    ("", {}),
    ("GN:Kavin", {"gn": "Kavin", "kavin": "Kavin"}),
    ("  GN : Kavin  ", {"gn": "Kavin", "kavin": "Kavin"}),
    ("GN:Kavin,Old Name:New", {"gn": "Kavin", "kavin": "Kavin",
                               "oldname": "New", "new": "New"}),
    # Renamed twice: every retired name still points at the current label.
    ("GN:Kevin,Kavin:Kevin", {"gn": "Kevin", "kevin": "Kevin", "kavin": "Kevin"}),
    ("GN", {}),          # no separator — not a pair
    ("GN:", {}),         # half a pair is not a rename
    (":Kavin", {}),
])
def test_alias_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("ROSTER_ALIASES", raw)
    assert ba._alias_map() == expected


def test_the_retired_name_still_resolves_after_the_roll_call_moves_on(monkeypatch):
    """The roll-call says "Kevin" now; a year of "G N" history must not strand."""
    monkeypatch.setenv("ROSTER_ALIASES", "GN:Kevin,Kavin:Kevin")
    mapping = ba.roster_name_map(["Kevin", "Soma"])
    assert ba._short_of("G N", mapping) == "Kevin"
    assert ba._short_of("KAVIN G N", mapping) == "Kevin"
    assert ba._short_of("Kavin", mapping) == "Kevin"
    assert ba._short_of("Soma Pani", mapping) == "Soma"


def test_the_new_name_resolves_before_the_roll_call_starts_using_it(monkeypatch):
    """The roll-call still says "GN"; check-ins already arrive as "Kavin"."""
    monkeypatch.setenv("ROSTER_ALIASES", "GN:Kevin,Kavin:Kevin")
    mapping = ba.roster_name_map(["GN", "Soma"])
    assert ba._short_of("Kavin", mapping) == "Kevin"
    assert ba._short_of("G N", mapping) == "Kevin"


def test_the_roll_call_name_and_the_slack_name_can_differ(monkeypatch):
    """27-08-2026: the roll-call says "Kevin", every check-in arrives as "Kavin".

    Neither spelling is the other's, so without the chain the day splits in two:
    a roster row present all day with no work, and a day of work on no roster.
    """
    monkeypatch.setenv("ROSTER_ALIASES", "GN:Kevin,Kavin:Kevin")
    mapping = ba.roster_name_map(["Kevin", "Soma", "Madhan"])
    assert ba._short_of("Kevin", mapping) == "Kevin"      # the roll-call token
    assert ba._short_of("Kavin", mapping) == "Kevin"      # the check-in author


def test_roster_name_map_without_aliases_is_just_the_roster(monkeypatch):
    monkeypatch.delenv("ROSTER_ALIASES", raising=False)
    assert ba.roster_name_map(["Soma", "Raghul"]) == {"soma": "Soma", "raghul": "Raghul"}


def test_master_seed_carries_the_current_name():
    """The Master tab seeds employee details; a stale label misfiles payslips."""
    names = [row[0] for row in ba.MASTER_SEED]
    assert "Kevin" in names
    assert "GN" not in names and "Kavin" not in names


# --------------------------------------------------------------------------
# Which of two accounts gets the DM
# --------------------------------------------------------------------------
def test_the_dm_goes_to_the_account_that_owns_the_roster_name(monkeypatch):
    """Membership order put the retired account first and it won the DM."""
    monkeypatch.setenv("ROSTER_ALIASES", "GN:Kevin,Kavin:Kevin")
    mapping = ba.roster_name_map(["Kevin", "Soma"])
    members = [("U0AMMMQC4Q2", "KAVIN G N"),      # retired, listed first
               ("U0BS24Y9MLK", "Kavin"),
               ("U08C4R9CCJU", "Soma Pani")]
    assert ba.pick_accounts(members, mapping)["Kevin"] == "U0BS24Y9MLK"


def test_the_live_account_wins_though_it_is_not_spelt_like_the_label(monkeypatch):
    """The label is "Kevin" and no Slack account is called that.

    Requiring the account's name to equal the label picks neither, drops back to
    membership order, and hands the DM to the retired login again — the 26-08
    bug, brought back by a rename. "Kavin" is a name the map knows, being the
    alias key for the label; "KAVIN G N" is nobody's name and got there on a
    prefix.
    """
    monkeypatch.setenv("ROSTER_ALIASES", "GN:Kevin,Kavin:Kevin")
    mapping = ba.roster_name_map(["Kevin"])
    members = [("U0AMMMQC4Q2", "KAVIN G N"), ("U0BS24Y9MLK", "Kavin")]
    assert ba.pick_accounts(members, mapping)["Kevin"] == "U0BS24Y9MLK"


def test_the_order_of_the_two_accounts_does_not_matter(monkeypatch):
    monkeypatch.setenv("ROSTER_ALIASES", "GN:Kevin,Kavin:Kevin")
    mapping = ba.roster_name_map(["Kevin"])
    forward = [("U0BS24Y9MLK", "Kavin"), ("U0AMMMQC4Q2", "KAVIN G N")]
    assert ba.pick_accounts(forward, mapping)["Kevin"] == "U0BS24Y9MLK"


def test_one_account_still_resolves_without_an_exact_match(monkeypatch):
    """"Malleshwaran M" never equals "Mallesh"; first-seen must still win."""
    monkeypatch.delenv("ROSTER_ALIASES", raising=False)
    mapping = ba.roster_name_map(["Mallesh"])
    got = ba.pick_accounts([("U0BEK6E250V", "Malleshwaran M")], mapping)
    assert got["Mallesh"] == "U0BEK6E250V"
