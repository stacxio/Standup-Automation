"""Compare Slack stand-ups against the meeting transcript and report the gaps.

One batched reasoning-engine call: given each developer's Slack stand-up text
for the day and the full meeting transcript, the engine identifies per-developer
gaps — things discussed in the meeting but missing from Slack (and vice versa),
and status/blocker mismatches. The deterministic signal (did they post in Slack)
is decided here, not by the engine, so it is always right; the semantic
comparison is the engine's job.
"""

from __future__ import annotations

import json
import re

from .engines import Engine

# Per-developer output fields. in_slack is overwritten deterministically after
# the engine call; the rest are the engine's judgement.
FIELDS = ["developer", "in_slack", "in_meeting", "assessment", "gaps"]
ASSESSMENTS = {"aligned", "minor gaps", "significant gaps", "no update"}

_SYSTEM = (
    "You audit a software team's daily stand-up. For each developer you compare\n"
    "what they wrote in their Slack stand-up against what was actually said in\n"
    "the team meeting transcript, and report the GAPS between the two.\n"
    "\n"
    "Input JSON: {developers: [{developer, slack}], transcript}. 'slack' is the\n"
    "developer's Slack stand-up text for the day (\"\" if they did not post).\n"
    "'transcript' is the full meeting transcript with speaker labels.\n"
    "\n"
    "For EACH developer output exactly one JSON object with fields:\n"
    "  developer   : copy through EXACTLY as given.\n"
    "  in_slack    : true if their 'slack' text is non-empty, else false.\n"
    "  in_meeting  : true if the transcript shows this person spoke or their\n"
    "                work was clearly discussed, else false.\n"
    "  assessment  : one of \"aligned\", \"minor gaps\", \"significant gaps\",\n"
    "                \"no update\" (used only when they neither posted nor spoke).\n"
    "  gaps        : array of short strings, each ONE concrete discrepancy, e.g.\n"
    "                \"Raised a deploy blocker in the meeting but not in Slack\"\n"
    "                or \"Slack lists ST-22 but it was not mentioned in the\n"
    "                meeting\". Use [] when aligned or when there is no update.\n"
    "\n"
    "Rules:\n"
    "- Report only real discrepancies grounded in the given text; invent nothing.\n"
    "- Speaker labels may be imperfect; match on names and context.\n"
    "- Return ONLY a JSON array, same order and length as 'developers'. No prose,\n"
    "  no code fences.\n"
)


def _norm(name: str) -> str:
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def _matches_speaker(dev: str, speakers: list[str]) -> bool:
    """Loose match between a roster name and a transcript speaker label."""
    d = _norm(dev)
    return bool(d) and any(
        (s := _norm(sp)) and (d.startswith(s) or s.startswith(d) or d in s or s in d)
        for sp in speakers
    )


def analyse(slack_by_dev: dict[str, str], transcript_text: str, engine: Engine,
            max_retries: int = 3, speakers: list[str] | None = None) -> list[dict]:
    """Return one gap record per developer (roster order preserved).

    `speakers` are the transcript's detected speaker labels; a developer whose
    name matches one is deterministically marked in_meeting, so a model miss on
    a clearly-present speaker can't wrongly downgrade them to 'no update'.
    """
    speakers = speakers or []
    developers = [{"developer": name, "slack": (slack_by_dev.get(name) or "").strip()}
                  for name in slack_by_dev]
    if not developers:
        return []

    payload = {"developers": developers, "transcript": transcript_text or ""}
    user = json.dumps(payload, ensure_ascii=False)

    last_error: Exception | None = None
    for _ in range(max(1, max_retries)):
        raw = engine.complete(_SYSTEM, user)
        try:
            records = _parse(raw)
            break
        except Exception as exc:  # noqa: BLE001 — retry on malformed output
            last_error = exc
    else:
        raise RuntimeError(f"Reasoning engine returned unusable output: {last_error}")

    # Trust our own signal for in_slack, and normalise assessment.
    by_name = {r["developer"]: r for r in records}
    out: list[dict] = []
    for d in developers:
        r = by_name.get(d["developer"], {})
        in_slack = bool(d["slack"])
        in_meeting = bool(r.get("in_meeting")) or _matches_speaker(d["developer"], speakers)
        gaps = [g for g in (r.get("gaps") or []) if isinstance(g, str) and g.strip()]
        assessment = str(r.get("assessment", "") or "").strip().lower()
        if not in_slack and not in_meeting:
            assessment, gaps = "no update", []
        elif assessment not in ASSESSMENTS:
            assessment = "significant gaps" if gaps else "aligned"
        out.append({
            "developer": d["developer"],
            "in_slack": in_slack,
            "in_meeting": in_meeting,
            "assessment": assessment,
            "gaps": [g.strip() for g in gaps],
        })
    return out


def _parse(raw: str) -> list[dict]:
    """Extract the JSON array from the engine reply (mirrors summarize._parse)."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON array in engine output")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, list):
        raise ValueError("engine output is not a JSON array")
    records: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("array item is not an object")
        records.append({
            "developer": str(item.get("developer", "") or "").strip(),
            "in_meeting": bool(item.get("in_meeting")),
            "assessment": str(item.get("assessment", "") or "").strip(),
            "gaps": item.get("gaps") or [],
        })
    return records
