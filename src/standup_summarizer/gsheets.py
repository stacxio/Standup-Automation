"""Opening and calling Google Sheets, with retries for transient failures.

Google returns 503 often enough that an unattended weekday job will meet one.
When it happened at 22:29 on 2026-08-20 the attendance step failed outright and
that day's sheet was never refreshed — one bad minute at Google cost a day of
data, with nothing wrong on our side.

Only failures that are worth trying again are retried:

  * 429  rate limited — the caller is fine, it just needs to wait
  * 5xx  Google is having a moment
  * transport errors — DNS, connection reset, timeout

A 401/403/404 is a real answer (bad credentials, no access, wrong id) and is
raised immediately: retrying it would turn a clear error into a slow one.

Writes here are safe to repeat. The agents write by clearing a tab and setting
its values, or by upserting on a key, so a retry after a partial write lands on
the same result rather than duplicating rows.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Statuses where trying again is the right move.
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
ATTEMPTS = 4
BASE_DELAY = 2.0


def _status_of(exc: Exception) -> int | None:
    """The HTTP status behind a gspread APIError, if there is one."""
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


def is_transient(exc: Exception) -> bool:
    """Whether `exc` is worth another attempt."""
    status = _status_of(exc)
    if status is not None:
        return status in RETRY_STATUS
    # No status at all means it never reached Google: DNS, reset, timeout.
    name = type(exc).__name__
    return name in {"TransportError", "ConnectionError", "ReadTimeout",
                    "ConnectTimeout", "Timeout", "ChunkedEncodingError"}


def retry_api(call: Callable[[], T], *, describe: str = "Sheets call",
              attempts: int = ATTEMPTS, base_delay: float = BASE_DELAY,
              sleep: Callable[[float], None] = time.sleep,
              log: Callable[[str], None] = print) -> T:
    """Run `call`, retrying transient Google failures with exponential backoff.

    Raises the last exception once the attempts are spent, so a genuine outage
    still fails the step rather than hanging the run.
    """
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 — re-raised below unless transient
            if attempt >= attempts or not is_transient(exc):
                raise
            delay = base_delay * (2 ** (attempt - 1))
            log(f"  {describe} failed ({type(exc).__name__}: {exc}); "
                f"retry {attempt}/{attempts - 1} in {delay:g}s")
            sleep(delay)
    raise AssertionError("unreachable")


def open_spreadsheet(spreadsheet_id: str, key_path: str | Path, **kw):
    """Authorise with the service account and open the sheet, with retries."""
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(str(key_path), scopes=SCOPES)
    client = gspread.authorize(creds)
    return retry_api(lambda: client.open_by_key(spreadsheet_id),
                     describe=f"open spreadsheet {spreadsheet_id[:8]}…", **kw)
