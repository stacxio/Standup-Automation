"""Scrum-Master verification normalisation (offline, fake engine)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from standup_summarizer import verify  # noqa: E402


class FakeEngine:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    def complete(self, system: str, user: str) -> str:
        return self.reply


ISSUES = [
    {"key": "STX-101", "summary": "Login API", "status": "In Progress", "priority": "High",
     "description": "Create login endpoint. AC: Login API created; JWT validation; unit tests",
     "comments": [], "attachments": ["Login_API_Design.pdf"], "pull_requests": []},
    {"key": "STX-110", "summary": "Logout", "status": "To Do", "priority": "Low",
     "description": "", "comments": [], "attachments": [], "pull_requests": []},
]


def test_normalises_and_preserves_issue_order():
    reply = """```json
    {
      "developer":"John",
      "issues":[
        {"key":"STX-101","discussed":true,"description_reflected":"yes",
         "comments_reflected":"yes","acceptance_progress":"partial","pr_mentioned":"yes",
         "attachments_referenced":"yes","blocker_mentioned":false,
         "notes":["Unit testing not mentioned"]},
        {"key":"STX-110","discussed":false,"description_reflected":"na",
         "comments_reflected":"na","acceptance_progress":"na","pr_mentioned":"na",
         "attachments_referenced":"na","blocker_mentioned":false,"notes":[]}
      ],
      "additional_work":["Mentioned deployment planning"],
      "coverage":85,"overall_status":"Partial Coverage",
      "recommendation":"Update Jira for deployment planning and discuss STX-110."
    }
    ```"""
    out = verify.verify_developer("John", ISSUES, "transcript", FakeEngine(reply))

    assert [c["key"] for c in out["issues"]] == ["STX-101", "STX-110"]  # input order
    assert out["issues"][0]["acceptance_progress"] == "partial"
    assert out["issues"][0]["notes"] == ["Unit testing not mentioned"]
    assert out["issues"][1]["discussed"] is False
    assert out["coverage"] == 85 and out["overall_status"] == "Partial Coverage"
    assert out["additional_work"] == ["Mentioned deployment planning"]


def test_missing_issue_in_reply_defaults_to_not_discussed():
    reply = '{"developer":"John","issues":[],"coverage":0,"recommendation":""}'
    out = verify.verify_developer("John", ISSUES, "t", FakeEngine(reply))
    assert len(out["issues"]) == 2  # one per assigned issue, even if engine omitted them
    assert all(c["discussed"] is False for c in out["issues"])


def test_bad_enum_and_coverage_are_coerced():
    reply = ('{"developer":"J","issues":[{"key":"STX-101","discussed":true,'
             '"description_reflected":"maybe","pr_mentioned":"","acceptance_progress":"partial"}],'
             '"coverage":250,"overall_status":"Bogus","recommendation":"x"}')
    out = verify.verify_developer("J", ISSUES[:1], "t", FakeEngine(reply))
    c = out["issues"][0]
    assert c["description_reflected"] == "na"   # invalid enum -> na
    assert c["pr_mentioned"] == "na"
    assert c["acceptance_progress"] == "partial"
    assert out["coverage"] == 100              # clamped
    assert out["overall_status"] == "Full Coverage"  # derived from coverage


def test_no_issues_short_circuits_without_engine_call():
    class Boom:
        def complete(self, system, user):
            raise AssertionError("engine must not be called when there are no issues")

    out = verify.verify_developer("Idle", [], "t", Boom())
    assert out["overall_status"] == "No assigned issues" and out["coverage"] == 0


def test_spoken_keys_attributes_issues_to_the_matching_speaker():
    import verify_standup as vs

    segments = [
        {"speaker": "GN", "text": "Recreated the kiosk UI, that's BHA-88, PR raised"},
        {"speaker": "Raghul", "text": "Working on HIR-75 and HIR - 76 today"},
        {"speaker": "", "text": "unattributed chatter mentioning WS-1"},
    ]
    out = vs.spoken_keys(segments, ["GN", "Soma", "Raghul", "Sahil"])
    assert out["GN"] == ["BHA-88"]
    assert out["Raghul"] == ["HIR-75", "HIR-76"]  # spaced key normalised too
    assert out["Soma"] == [] and out["Sahil"] == []  # unattributed keys are dropped
