"""Transcript parsing + Slack-vs-transcript gap analysis (offline, fake engine)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from standup_summarizer import gap, transcript  # noqa: E402

OTTER = """Soma Pani  0:03
Yesterday I finished the login screen and started
the API client. No blockers.

Raghul  1:20
I closed the DOB bugs and deployed to prod. There is a
blocker on the affiliate providers.
"""


class FakeEngine:
    """Returns a canned JSON array; records the prompt it was given."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.system = self.user = None

    def complete(self, system: str, user: str) -> str:
        self.system, self.user = system, user
        return self.reply


# --- transcript parsing ---------------------------------------------------
def test_parses_speaker_segments():
    segs = transcript.parse_transcript(OTTER)
    assert [s["speaker"] for s in segs] == ["Soma Pani", "Raghul"]
    assert "API client" in segs[0]["text"]
    assert "\n" not in segs[0]["text"]  # multi-line speech is joined
    assert transcript.speakers(segs) == ["Soma Pani", "Raghul"]


def test_plain_paste_without_headers_is_one_segment():
    segs = transcript.parse_transcript("just some notes with no speaker headers at all")
    assert len(segs) == 1 and segs[0]["speaker"] == ""


def test_sentence_ending_in_time_is_not_a_header():
    text = "We agreed the whole team should sync again at 3:30\nand ship by friday"
    segs = transcript.parse_transcript(text)
    assert len(segs) == 1 and segs[0]["speaker"] == ""  # not mistaken for a header


def test_render_roundtrip_labels_unknown():
    segs = [{"speaker": "", "text": "hello"}]
    assert transcript.render(segs) == "Unknown: hello"


# --- gap analysis ---------------------------------------------------------
def test_analyse_overrides_in_slack_and_keeps_order():
    reply = """[
      {"developer":"Soma","in_meeting":true,"assessment":"aligned","gaps":[]},
      {"developer":"Raghul","in_meeting":true,"assessment":"significant gaps",
       "gaps":["Raised an affiliate-provider blocker in the meeting not in Slack"]}
    ]"""
    engine = FakeEngine(reply)
    standups = {"Soma": "finished login, started API client", "Raghul": "closed DOB bugs"}
    out = gap.analyse(standups, "transcript text", engine)

    assert [r["developer"] for r in out] == ["Soma", "Raghul"]  # roster order kept
    assert out[0]["in_slack"] is True and out[1]["in_slack"] is True
    assert out[1]["assessment"] == "significant gaps"
    assert out[1]["gaps"] == ["Raised an affiliate-provider blocker in the meeting not in Slack"]


def test_no_update_when_absent_from_both():
    reply = '[{"developer":"Ghost","in_meeting":false,"assessment":"aligned","gaps":["x"]}]'
    out = gap.analyse({"Ghost": ""}, "", FakeEngine(reply))
    assert out[0]["assessment"] == "no update"  # forced: no slack + no meeting
    assert out[0]["in_slack"] is False and out[0]["gaps"] == []


def test_unknown_assessment_falls_back_by_gap_presence():
    reply = '[{"developer":"A","in_meeting":true,"assessment":"weird","gaps":["a gap"]}]'
    out = gap.analyse({"A": "posted"}, "t", FakeEngine(reply))
    assert out[0]["assessment"] == "significant gaps"

    reply2 = '[{"developer":"A","in_meeting":true,"assessment":"weird","gaps":[]}]'
    out2 = gap.analyse({"A": "posted"}, "t", FakeEngine(reply2))
    assert out2[0]["assessment"] == "aligned"


def test_detected_speaker_overrides_model_in_meeting():
    """A model 'in_meeting: false' is overridden when the dev is a known speaker."""
    reply = '[{"developer":"Raghul","in_meeting":false,"assessment":"aligned","gaps":[]}]'
    # No Slack post, but Raghul is a detected transcript speaker -> in_meeting True,
    # so it must NOT collapse to "no update".
    out = gap.analyse({"Raghul": ""}, "t", FakeEngine(reply), speakers=["Raghul"])
    assert out[0]["in_meeting"] is True
    assert out[0]["assessment"] != "no update"


def test_speaker_match_is_loose():
    reply = '[{"developer":"Soma","in_meeting":false,"assessment":"aligned","gaps":[]}]'
    out = gap.analyse({"Soma": "posted"}, "t", FakeEngine(reply), speakers=["Soma Pani"])
    assert out[0]["in_meeting"] is True


def test_empty_roster_returns_empty():
    assert gap.analyse({}, "t", FakeEngine("[]")) == []
