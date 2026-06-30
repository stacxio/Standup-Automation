"""Summarize stage — grouped stand-up notes -> structured records (FR-7..FR-12).

One batched reasoning-engine call per run (FR-7, NFR-1): all entries go in a
single prompt and the engine returns a JSON array, one object per entry. Output
is validated against the field schema and retried on malformed output (FR-9,
FR-12). Missing fields become "" so a record is always emitted (FR-10).
"""

from __future__ import annotations

import json
import re

from .engines import Engine

# The structured fields the engine must produce for each entry.
FIELDS = [
    "developer", "date", "project", "task", "value",
    "what_got_moved", "why_it_matters", "blockers", "whats_next",
]

_SYSTEM = (
    "You convert daily stand-up / check-in notes into structured JSON.\n"
    "You receive a JSON array of entries, each {developer, date, text}.\n"
    "For EACH entry output exactly one JSON object with these string fields:\n"
    "  developer, date, project, task, value, what_got_moved, why_it_matters,\n"
    "  blockers, whats_next\n"
    "Rules:\n"
    "- Copy 'developer' and 'date' through EXACTLY as given.\n"
    "- Summarise each field concisely from the note; do not invent facts.\n"
    "- If a field has no information, use an empty string \"\" (for blockers use \"None\").\n"
    "- Return ONLY a JSON array (same length/order as the input). No prose, no code fences."
)


def summarize(entries: list[dict], engine: Engine, max_retries: int = 3) -> list[dict]:
    """Return one structured record per entry via a single batched engine call."""
    if not entries:
        return []
    user = "Entries:\n" + json.dumps(entries, ensure_ascii=False)
    last_error: Exception | None = None
    for _ in range(max(1, max_retries)):
        raw = engine.complete(_SYSTEM, user)
        try:
            return _parse(raw)
        except Exception as exc:  # noqa: BLE001 — retry on any malformed output
            last_error = exc
    raise RuntimeError(f"Reasoning engine returned unusable output: {last_error}")


def _parse(raw: str) -> list[dict]:
    """Extract and normalize the JSON array from the engine's reply."""
    text = (raw or "").strip()
    if text.startswith("```"):  # strip ```json ... ``` fences
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
        records.append({f: str(item.get(f, "") or "").strip() for f in FIELDS})
    return records
