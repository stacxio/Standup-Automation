"""Opening a Google spreadsheet, retrying while the connection settles.

The retry policy itself lives in `retrying`, shared with the Slack calls: both
services fail the same way and one policy is easier to reason about than two.
"""

from __future__ import annotations

from pathlib import Path

from .retrying import retry_api

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

__all__ = ["SCOPES", "open_spreadsheet", "retry_api"]


def open_spreadsheet(spreadsheet_id: str, key_path: str | Path, **kw):
    """Authorise with the service account and open the sheet, with retries."""
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(str(key_path), scopes=SCOPES)
    client = gspread.authorize(creds)
    return retry_api(lambda: client.open_by_key(spreadsheet_id),
                     describe=f"open spreadsheet {spreadsheet_id[:8]}…", **kw)
