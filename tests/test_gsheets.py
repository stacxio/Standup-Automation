"""Retrying transient Google and Slack failures (src/standup_summarizer/retrying.py)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from standup_summarizer import retrying as gsheets  # noqa: E402


class _Response:
    def __init__(self, status_code):
        self.status_code = status_code


class APIError(Exception):
    """Shaped like gspread.exceptions.APIError: carries a .response."""

    def __init__(self, status_code):
        super().__init__(f"APIError: [{status_code}]")
        self.response = _Response(status_code)


class TransportError(Exception):
    """Shaped like google.auth.exceptions.TransportError: no status at all."""


class _Flaky:
    """Fails `fails` times, then returns `value`."""

    def __init__(self, exc, fails, value="ok"):
        self.exc, self.fails, self.value, self.calls = exc, fails, value, 0

    def __call__(self):
        self.calls += 1
        if self.calls <= self.fails:
            raise self.exc
        return self.value


def _run(call, **kw):
    slept = []
    result = gsheets.retry_api(call, sleep=slept.append, log=lambda _m: None,
                               base_delay=1.0, **kw)
    return result, slept


# --- which failures are worth another try ---------------------------------
@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_statuses_are_retried(status):
    assert gsheets.is_transient(APIError(status)) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409])
def test_a_real_answer_is_not_retried(status):
    """Bad credentials or a wrong id will not improve by asking again."""
    assert gsheets.is_transient(APIError(status)) is False


def test_a_failure_that_never_reached_google_is_retried():
    """DNS and connection resets carry no status."""
    assert gsheets.is_transient(TransportError("getaddrinfo failed")) is True


def test_an_ordinary_bug_is_not_retried():
    assert gsheets.is_transient(ValueError("bad row")) is False
    assert gsheets.is_transient(KeyError("missing")) is False


# --- retrying -------------------------------------------------------------
def test_a_call_that_works_first_time_is_made_once():
    call = _Flaky(APIError(503), fails=0)
    assert _run(call)[0] == "ok"
    assert call.calls == 1


def test_the_503_that_broke_the_attendance_step_now_recovers():
    call = _Flaky(APIError(503), fails=2)
    result, slept = _run(call)
    assert result == "ok" and call.calls == 3


def test_backoff_grows_between_attempts():
    _, slept = _run(_Flaky(APIError(503), fails=3), attempts=5)
    assert slept == [1.0, 2.0, 4.0]          # doubling, not a fixed pause


def test_a_persistent_outage_still_fails_the_step():
    """Retrying forever would hang the run; the last error is raised."""
    call = _Flaky(APIError(503), fails=99)
    with pytest.raises(APIError):
        _run(call, attempts=3)
    assert call.calls == 3


def test_a_permanent_error_fails_immediately_without_waiting():
    call = _Flaky(APIError(404), fails=99)
    with pytest.raises(APIError):
        _run(call)
    assert call.calls == 1                   # not retried at all


def test_the_value_is_returned_unchanged():
    assert _run(_Flaky(APIError(503), fails=1, value={"a": 1}))[0] == {"a": 1}


def test_a_transport_error_is_retried_then_succeeds():
    call = _Flaky(TransportError("getaddrinfo failed"), fails=1)
    assert _run(call)[0] == "ok" and call.calls == 2


def test_attempts_of_one_means_no_retry():
    call = _Flaky(APIError(503), fails=99)
    with pytest.raises(APIError):
        _run(call, attempts=1)
    assert call.calls == 1


def test_each_retry_is_reported_so_a_slow_run_is_explainable():
    messages = []
    with pytest.raises(APIError):
        gsheets.retry_api(_Flaky(APIError(503), fails=99), attempts=3,
                          base_delay=0, sleep=lambda _s: None,
                          log=messages.append, describe="write 'Scorecard Daily'")
    assert len(messages) == 2
    assert "write 'Scorecard Daily'" in messages[0] and "503" in messages[0]


# --- Slack failures ------------------------------------------------------
class SlackApiError(Exception):
    """Shaped like slack_sdk.errors.SlackApiError: .response is a mapping."""

    def __init__(self, status):
        super().__init__(f"server error {status}")
        self.response = {"status": status, "ok": False}


class URLError(Exception):
    """Shaped like urllib.error.URLError: the cause hides in .reason."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_a_slack_server_error_is_retried(status):
    assert gsheets.is_transient(SlackApiError(status)) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_a_slack_client_error_is_not_retried(status):
    """not_in_channel and missing_scope will not fix themselves."""
    assert gsheets.is_transient(SlackApiError(status)) is False


def test_the_tls_timeout_that_lost_the_digest_is_retried():
    """urllib.error.URLError: <urlopen error _ssl.c:993: handshake timed out>."""
    assert gsheets.is_transient(URLError(TimeoutError("handshake timed out"))) is True


def test_a_dns_failure_inside_urlerror_is_retried():
    class gaierror(OSError):
        pass

    assert gsheets.is_transient(URLError(gaierror("getaddrinfo failed"))) is True


def test_a_urlerror_with_an_ordinary_reason_is_not_retried():
    assert gsheets.is_transient(URLError("unknown url type")) is False


def test_a_slack_call_recovers_after_a_timeout():
    call = _Flaky(URLError(TimeoutError("handshake timed out")), fails=2)
    assert _run(call)[0] == "ok" and call.calls == 3
