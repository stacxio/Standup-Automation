"""Verify a developer's stand-up against their Jira context (Scrum-Master AI).

For one developer, the engine receives their assigned Jira issues — each with
summary, status, priority, description (acceptance criteria are embedded there,
this Jira has no dedicated field), latest comments, attachment names and pull
request info — plus the meeting transcript. It returns a structured verification:
per issue whether it was discussed and whether the spoken update reflects the
description, comments, acceptance criteria, PR and attachments; work mentioned
that isn't in Jira; a coverage score; and a manager-friendly recommendation.

One engine call per developer (their Jira context is specific to them). The
comparison is the engine's judgement; treat findings as review prompts, not
ground truth.
"""

from __future__ import annotations

import json
import re

from .engines import Engine

_SYSTEM = (
    "You are a Scrum Master auditing a developer's daily stand-up.\n"
    "\n"
    "You receive JSON with:\n"
    "  developer  : the developer's name.\n"
    "  issues     : their assigned Jira issues, each {key, summary, status,\n"
    "               priority, description, comments:[{author,body}],\n"
    "               attachments:[filename], pull_requests:[{url,status}]}.\n"
    "               Acceptance criteria, if any, are inside 'description'.\n"
    "  transcript : the developer's meeting transcript (may include others).\n"
    "\n"
    "Tasks:\n"
    "  1. Determine whether each Jira issue was discussed in the meeting.\n"
    "  2. Check whether the spoken update matches the Jira description.\n"
    "  3. Verify whether the latest Jira comments are reflected.\n"
    "  4. Identify progress mentioned toward the acceptance criteria.\n"
    "  5. Detect blockers or risks mentioned in the meeting.\n"
    "  6. Identify work mentioned in the meeting that is not in Jira.\n"
    "  7. Flag inconsistencies between Jira and the meeting.\n"
    "  8. Assign a coverage score 0-100.\n"
    "  9. Give a concise manager-friendly recommendation.\n"
    "\n"
    "Output ONLY one JSON object (no prose, no code fences):\n"
    "{\n"
    '  \"developer\": string,\n'
    '  \"issues\": [{\n'
    '    \"key\": string,\n'
    '    \"discussed\": true|false,\n'
    '    \"description_reflected\": \"yes\"|\"partial\"|\"no\"|\"na\",\n'
    '    \"comments_reflected\": \"yes\"|\"partial\"|\"no\"|\"na\",\n'
    '    \"acceptance_progress\": \"yes\"|\"partial\"|\"no\"|\"na\",\n'
    '    \"pr_mentioned\": \"yes\"|\"no\"|\"na\",\n'
    '    \"attachments_referenced\": \"yes\"|\"no\"|\"na\",\n'
    '    \"blocker_mentioned\": true|false,\n'
    '    \"notes\": [string]   // short, e.g. \"Unit testing not mentioned\"\n'
    "  }],\n"
    '  \"additional_work\": [string],   // discussed but not tracked in Jira\n'
    '  \"coverage\": 0-100,\n'
    '  \"overall_status\": \"Full Coverage\"|\"Partial Coverage\"|\"Low Coverage\",\n'
    '  \"recommendation\": string\n'
    "}\n"
    "Ground every finding in the given text; invent nothing. Use \"na\" when a\n"
    "check does not apply (e.g. no PR on the issue, no attachments).\n"
)

_ISSUE_KEYS = {
    "key", "discussed", "description_reflected", "comments_reflected",
    "acceptance_progress", "pr_mentioned", "attachments_referenced",
    "blocker_mentioned", "notes",
}
_ENUM = {"yes", "partial", "no", "na"}


def verify_developer(developer: str, issues: list[dict], transcript_text: str,
                     engine: Engine, max_retries: int = 3) -> dict:
    """Return the verification record for one developer (see module docstring)."""
    if not issues:
        return {
            "developer": developer, "issues": [], "additional_work": [],
            "coverage": 0, "overall_status": "No assigned issues",
            "recommendation": "No active assigned Jira issues in the window to verify.",
        }

    payload = {"developer": developer, "issues": issues, "transcript": transcript_text or ""}
    user = json.dumps(payload, ensure_ascii=False)

    last_error: Exception | None = None
    for _ in range(max(1, max_retries)):
        raw = engine.complete(_SYSTEM, user)
        try:
            return _normalise(_parse(raw), developer, issues)
        except Exception as exc:  # noqa: BLE001 — retry on malformed output
            last_error = exc
    raise RuntimeError(f"Reasoning engine returned unusable output: {last_error}")


def _parse(raw: str) -> dict:
    """Extract the JSON object from the engine reply."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object in engine output")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("engine output is not a JSON object")
    return data


def _enum(value: str, default: str = "na") -> str:
    v = str(value or "").strip().lower()
    return v if v in _ENUM else default


def _normalise(data: dict, developer: str, issues: list[dict]) -> dict:
    """Coerce the engine object into the fixed schema; keep issue order/keys."""
    raw_issues = {str(i.get("key", "")): i for i in (data.get("issues") or []) if isinstance(i, dict)}
    checks = []
    for ctx in issues:  # preserve the input order, one check per assigned issue
        key = ctx["key"]
        r = raw_issues.get(key, {})
        checks.append({
            "key": key,
            "discussed": bool(r.get("discussed")),
            "description_reflected": _enum(r.get("description_reflected")),
            "comments_reflected": _enum(r.get("comments_reflected")),
            "acceptance_progress": _enum(r.get("acceptance_progress")),
            "pr_mentioned": _enum(r.get("pr_mentioned")),
            "attachments_referenced": _enum(r.get("attachments_referenced")),
            "blocker_mentioned": bool(r.get("blocker_mentioned")),
            "notes": [str(n).strip() for n in (r.get("notes") or []) if str(n).strip()],
        })

    try:
        coverage = max(0, min(100, int(round(float(data.get("coverage", 0))))))
    except (TypeError, ValueError):
        coverage = 0
    status = str(data.get("overall_status", "") or "").strip()
    if status not in {"Full Coverage", "Partial Coverage", "Low Coverage"}:
        status = "Full Coverage" if coverage >= 90 else "Partial Coverage" if coverage >= 50 else "Low Coverage"

    return {
        "developer": developer,
        "issues": checks,
        "additional_work": [str(w).strip() for w in (data.get("additional_work") or []) if str(w).strip()],
        "coverage": coverage,
        "overall_status": status,
        "recommendation": str(data.get("recommendation", "") or "").strip(),
    }
