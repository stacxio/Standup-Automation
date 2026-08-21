"""Retrying calls to Google and Slack when the failure is worth another try.

Both services fail the same way — a 5xx, a rate limit, or a connection that
never completes — so one policy covers both rather than two that drift apart.

Two nights running, a transient network error cost a step of the daily job:
Google answered 503 and the attendance sheet was never refreshed, then a Slack
TLS handshake timed out and the whole digest went unposted. Neither had anything
wrong on our side, and an unattended weekday job meets both often enough to plan
for.

Only failures worth repeating are retried:

  * 429  rate limited — the caller is fine, it just needs to wait
  * 5xx  the service is having a moment
  * a call that never completed — DNS, connection reset, TLS timeout

A 401/403/404 is a real answer (bad credentials, no access, wrong id) and is
raised immediately: retrying it would turn a clear error into a slow one.

Callers must be safe to repeat. Sheets writes clear a tab and set its values, or
upsert on a key; a Slack post is only retried when the failure means it never
arrived, so a retry cannot duplicate a message that was already delivered.
"""

from __future__ import annotations

import time
from typing import Callable, TypeVar

T = TypeVar("T")

# Statuses where trying again is the right move.
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
ATTEMPTS = 4
BASE_DELAY = 2.0

# Exception types that mean the request never completed.
#
# URLError is deliberately absent: urllib raises it both for a timed-out
# handshake and for "unknown url type", which is a mistake in the request that
# no amount of retrying will fix. It carries the real cause in .reason, so it is
# judged by that instead.
_INCOMPLETE = {
    "TransportError", "ConnectionError", "ReadTimeout", "ConnectTimeout",
    "Timeout", "TimeoutError", "ChunkedEncodingError", "SSLError",
    "SSLEOFError", "RemoteDisconnected", "IncompleteRead",
}
# The wrapped causes urllib reports through .reason.
_INCOMPLETE_CAUSES = {
    "timeout", "TimeoutError", "SSLError", "SSLEOFError", "SSLZeroReturnError",
    "gaierror", "ConnectionResetError", "ConnectionAbortedError",
    "ConnectionRefusedError",
}


def _status_of(exc: Exception) -> int | None:
    """The HTTP status behind the error, however the client reports it."""
    response = getattr(exc, "response", None)
    # gspread: an object with .status_code
    status = getattr(response, "status_code", None)
    if status is not None:
        return status
    # slack_sdk: a mapping with "status"
    if hasattr(response, "get"):
        try:
            return int(response.get("status") or 0) or None
        except (TypeError, ValueError):
            return None
    return None


def is_transient(exc: Exception) -> bool:
    """Whether `exc` is worth another attempt."""
    status = _status_of(exc)
    if status is not None:
        return status in RETRY_STATUS
    if type(exc).__name__ in _INCOMPLETE:
        return True
    reason = getattr(exc, "reason", None)
    return reason is not None and type(reason).__name__ in _INCOMPLETE_CAUSES


def retry_api(call: Callable[[], T], *, describe: str = "API call",
              attempts: int = ATTEMPTS, base_delay: float = BASE_DELAY,
              sleep: Callable[[float], None] = time.sleep,
              log: Callable[[str], None] = print) -> T:
    """Run `call`, retrying transient failures with exponential backoff.

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
