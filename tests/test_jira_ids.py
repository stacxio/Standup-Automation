"""Jira id extraction from hand-typed stand-up text (build_report.extract_jira_ids).

Stand-ups are written by hand, so the same issue shows up as "WS-186",
"WS - 186" or "BHA- 79". All three must normalise to one canonical key, while
ordinary prose containing "word - number" must not be mistaken for an issue.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from build_report import extract_jira_ids  # noqa: E402


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Task ID: WS-186 done", ["WS-186"]),
        ("*what is task:* HIR - 72 redesign", ["HIR-72"]),          # spaced
        ("What is task: BHA- 79 finalize e2e", ["BHA-79"]),         # half-spaced
        ("Subtask ID - WS - 186: Implement RAG", ["WS-186"]),
        ("HIR - 72 and HIR-73 and hir-72", ["HIR-72", "HIR-73"]),   # dedup + case
        ("TASK ID-SP-10 shipped", ["SP-10"]),
        ("", []),
        ("no ids here at all", []),
    ],
)
def test_extracts_and_normalises(text, expected):
    assert extract_jira_ids(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Redesign the Best for page - 2 today",   # prose, not a key
        "covered in a week - 50 contacts",
        "rotation 7 - 30d",
    ],
)
def test_spacing_not_allowed_for_prose(text):
    assert extract_jira_ids(text) == []


def test_unspaced_lowercase_still_matches():
    """Pre-existing behaviour: tight 'word-123' is accepted regardless of case."""
    assert extract_jira_ids("breach pi-01 humidity") == ["PI-01"]


# --- Slack italic markup glued to the key ---------------------------------
# Real miss (Soma, 01-09-2026): "_*Task ID:*_SP-28" scored 0 tasks for the day.
# `_` is a word character, so the leading word boundary never fires between
# the closing italic underscore and the key: the check-in read as "no task id".


@pytest.mark.parametrize(
    "text, expected",
    [
        ("_*Task ID:*_SP-28", ["SP-28"]),          # the real one
        ("_SP-28_", ["SP-28"]),                    # italic-wrapped key
        ("*_SP-28_*", ["SP-28"]),                  # bold + italic
        ("_SP-28", ["SP-28"]),                     # markup at string start
        ("_*Task ID:*_ SP-28", ["SP-28"]),         # spaced: worked before, still does
    ],
)
def test_markup_underscore_does_not_hide_a_key(text, expected):
    assert extract_jira_ids(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "refresh_emi_matview-2",   # snake_case identifier, not an issue key
        "run_repair_batch.py",
        "feature_data-deletion-fix",
    ],
)
def test_an_underscore_inside_a_word_is_not_markup(text):
    """An identifier's own underscore must stay put, or its tail reads as a key."""
    assert extract_jira_ids(text) == []


def test_order_is_first_appearance():
    text = "moved WS-179, WS-177 and WS-178"
    assert extract_jira_ids(text) == ["WS-179", "WS-177", "WS-178"]


# --- ADF flattening: urls must survive ------------------------------------
# Jira stores a pasted url as a "smart link" carrying no text, and hides a
# hyperlink's target in a mark. Both were dropped, so "PR: <pasted link>"
# flattened to "PR:" and no matcher could ever see the url.
from standup_summarizer import jira as _jira  # noqa: E402


def _doc(*content):
    return {"type": "doc", "version": 1, "content": list(content)}


def test_a_pasted_link_survives_flattening():
    """The real SP-20 comment: 'PR:' followed by an inlineCard."""
    url = "https://github.com/hirocomco/agb-admin-console/tree/main"
    body = _doc({"type": "paragraph", "content": [
        {"type": "text", "text": "PR: "},
        {"type": "inlineCard", "attrs": {"url": url}},
    ]})
    assert url in _jira.adf_to_text(body)


@pytest.mark.parametrize("card", ["inlineCard", "blockCard", "embedCard"])
def test_every_card_type_yields_its_url(card):
    body = _doc({"type": card, "attrs": {"url": "https://example.com/x"}})
    assert "https://example.com/x" in _jira.adf_to_text(body)


def test_a_hyperlinked_word_yields_both_its_text_and_its_target():
    body = _doc({"type": "paragraph", "content": [
        {"type": "text", "text": "see the PR",
         "marks": [{"type": "link", "attrs": {"href": "https://example.com/pull/9"}}]},
    ]})
    flat = _jira.adf_to_text(body)
    assert "see the PR" in flat and "https://example.com/pull/9" in flat


def test_a_card_without_a_url_adds_nothing():
    body = _doc({"type": "inlineCard", "attrs": {"localId": "abc"}})
    assert _jira.adf_to_text(body).strip() == ""


def test_ordinary_text_is_unchanged():
    body = _doc({"type": "paragraph", "content": [{"type": "text", "text": "plain words"}]})
    assert _jira.adf_to_text(body).strip() == "plain words"


def test_flattening_still_survives_junk():
    assert _jira.adf_to_text(None) == ""
    assert _jira.adf_to_text({"type": "paragraph"}) == ""
    assert _jira.adf_to_text([{"type": "text", "text": "a"}, None]) == "a"
