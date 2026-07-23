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


def test_order_is_first_appearance():
    text = "moved WS-179, WS-177 and WS-178"
    assert extract_jira_ids(text) == ["WS-179", "WS-177", "WS-178"]
