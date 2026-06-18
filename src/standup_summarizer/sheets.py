"""Push stage — write/upsert summary rows to Google Sheets (FR-13..FR-16).

- One row per person per day (FR-13).
- Upsert by person+date so re-runs don't duplicate (FR-14, NFR-4).
- Flag expected members who posted no update (FR-15).
- Use the appropriate value-input option for dates/status (FR-16).
"""
