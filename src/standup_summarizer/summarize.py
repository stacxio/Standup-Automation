"""Summarize stage — grouped messages -> structured summaries (FR-7..FR-12).

- One batched reasoning-engine call per run, not per person (FR-7, NFR-1).
- Per-person structured record: person, summary, status, blockers (FR-8).
- Engine returns JSON validated against the schema (FR-9).
- Ambiguous/empty -> still emit a record with a no-update status (FR-10).
- Backend selectable by config; delegates to engines/ (FR-11).
- Retry on malformed output up to a configurable limit (FR-12).
"""
