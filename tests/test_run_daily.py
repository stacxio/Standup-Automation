"""The daily orchestrator: log encoding, exit conventions, failure isolation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import run_daily as rd  # noqa: E402

# What the agents actually print: an em-dash, a middot, and a mangled-prone name.
UNICODE_LINE = "Scorecard — 2026-08-18 · team average 29.3 — Kavin GN"


def child(tmp_path: Path, body: str, name: str = "step.py") -> Path:
    script = tmp_path / name
    script.write_text(body, encoding="utf-8")
    return script


def run(monkeypatch, tmp_path: Path, steps: list[tuple],
        argv: list[str] | None = None) -> str:
    """Run main() with `steps` and return what landed in the log."""
    log = tmp_path / "daily.log"
    monkeypatch.setattr(rd, "LOG", log)
    monkeypatch.setattr(rd, "PY", Path(sys.executable))
    monkeypatch.setattr(rd, "STEPS", steps)
    with pytest.raises(SystemExit) as exit_info:
        rd.main(argv or ["run_daily.py"])
    run.code = exit_info.value.code
    return log.read_text(encoding="utf-8")


# --- log encoding ---------------------------------------------------------
def test_the_log_keeps_the_agents_unicode_intact(monkeypatch, tmp_path):
    """The regression: text=True without an encoding decoded UTF-8 as cp1252,
    so 'Scorecard — 2026-08-18' reached the log as 'Scorecard â€” 2026-08-18'."""
    script = child(tmp_path, f"print({UNICODE_LINE!r})\n")
    text = run(monkeypatch, tmp_path, [("emit", [str(script)], False)])
    assert UNICODE_LINE in text
    for mangled in ("â€", "Â·", "Ã¢"):
        assert mangled not in text


def test_agent_output_is_utf8_whatever_the_ambient_encoding(monkeypatch, tmp_path):
    """A stray PYTHONIOENCODING must not change what lands in the log."""
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    script = child(tmp_path, f"print({UNICODE_LINE!r})\n")
    assert UNICODE_LINE in run(monkeypatch, tmp_path, [("emit", [str(script)], False)])


def test_step_env_pins_the_child_encoding_without_dropping_the_environment():
    monkeypatch_free = rd.step_env()
    assert monkeypatch_free["PYTHONIOENCODING"] == "utf-8"
    assert "PATH" in monkeypatch_free      # inherits, does not replace


def test_undecodable_bytes_do_not_lose_the_step(monkeypatch, tmp_path):
    """errors='replace' — a stray byte must not cost us the whole step's output."""
    script = child(tmp_path, "import sys\n"
                             "sys.stdout.buffer.write(b'before \\xff\\xfe after\\n')\n")
    text = run(monkeypatch, tmp_path, [("emit", [str(script)], False)])
    assert "before" in text and "after" in text


# --- exit conventions -----------------------------------------------------
def test_exit_two_is_a_skip_not_a_failure(monkeypatch, tmp_path):
    script = child(tmp_path, "raise SystemExit(2)\n")
    run(monkeypatch, tmp_path, [("skipper", [str(script)], False)])
    assert run.code == 0


def test_a_failing_step_is_counted_and_logged(monkeypatch, tmp_path):
    script = child(tmp_path, "import sys; print('boom'); raise SystemExit(1)\n")
    text = run(monkeypatch, tmp_path, [("bad", [str(script)], False)])
    assert run.code == 1
    assert "--- bad (exit 1) ---" in text and "boom" in text


def test_one_failing_step_does_not_stop_the_rest(monkeypatch, tmp_path):
    bad = child(tmp_path, "raise SystemExit(1)\n", "bad.py")
    good = child(tmp_path, "print('ran anyway')\n", "good.py")
    text = run(monkeypatch, tmp_path, [("bad", [str(bad)], False), ("good", [str(good)], False)])
    assert run.code == 1                      # the failure is still reported
    assert "ran anyway" in text               # ...but the later step still ran


def test_a_crashing_launch_is_caught_rather_than_aborting_the_run(monkeypatch, tmp_path):
    missing = tmp_path / "does_not_exist.py"
    good = child(tmp_path, "print('still ran')\n", "good.py")
    text = run(monkeypatch, tmp_path, [("gone", [str(missing)], False), ("good", [str(good)], False)])
    assert "still ran" in text


def test_every_run_is_appended_under_a_dated_header(monkeypatch, tmp_path):
    script = child(tmp_path, "print('one')\n")
    run(monkeypatch, tmp_path, [("emit", [str(script)], False)])
    text = run(monkeypatch, tmp_path, [("emit", [str(script)], False)])
    assert text.count("daily run =====") == 2     # history accumulates
    assert text.count("one") == 2


# --- the pipeline itself --------------------------------------------------
def test_the_scorecard_and_dashboard_run_after_the_report():
    """Scoring reads the Jira state the report step has just refreshed."""
    labels = [label for label, *_ in rd.STEPS]
    assert labels == ["attendance", "report", "summary", "scorecard", "dashboard"]


def test_the_scorecard_step_posts_and_the_dashboard_does_not():
    steps = {label: args for label, args, _ in rd.STEPS}
    assert steps["scorecard"] == ["build_scorecard.py", "--score", "--notify"]
    assert "--notify" not in steps["dashboard"]


def test_every_step_names_a_script_that_exists():
    for _label, args, _dated in rd.STEPS:
        assert (ROOT / args[0]).exists(), args[0]


# --- run date -------------------------------------------------------------
AUG19 = "19-08-2026"


def _recent(days_ago: int) -> str:
    import datetime as _dt
    return (_dt.date.today() - _dt.timedelta(days=days_ago)).strftime("%d-%m-%Y")


def test_no_argument_means_today():
    import datetime as _dt
    assert rd.resolve_day(["run_daily.py"]) == _dt.date.today()


@pytest.mark.parametrize("argv", [
    ["run_daily.py", "19-08-2026"],            # positional, as a person types it
    ["run_daily.py", "--date", "19-08-2026"],
    ["run_daily.py", "--date=2026-08-19"],
])
def test_a_date_is_accepted_positionally_or_by_flag(argv):
    import datetime as _dt
    if (_dt.date.today() - _dt.date(2026, 8, 19)).days > rd.LOOKBACK_DAYS:
        pytest.skip("fixture date has aged out of the lookback window")
    assert rd.resolve_day(argv) == _dt.date(2026, 8, 19)


def test_a_future_date_is_refused():
    import datetime as _dt
    ahead = (_dt.date.today() + _dt.timedelta(days=1)).strftime("%d-%m-%Y")
    with pytest.raises(SystemExit) as exc:
        rd.resolve_day(["run_daily.py", ahead])
    assert "future" in str(exc.value)


def test_a_date_beyond_the_slack_history_is_refused_not_run_empty():
    """A backfill that finds nothing must stop, not look successful."""
    with pytest.raises(SystemExit) as exc:
        rd.resolve_day(["run_daily.py", _recent(rd.LOOKBACK_DAYS + 1)])
    assert "beyond" in str(exc.value)


def test_the_edge_of_the_window_is_still_allowed():
    assert rd.resolve_day(["run_daily.py", _recent(rd.LOOKBACK_DAYS)])


def test_an_unparseable_date_stops_the_run():
    with pytest.raises(SystemExit) as exc:
        rd.resolve_day(["run_daily.py", "--date", "yesterday"])
    assert "DD-MM-YYYY" in str(exc.value)


# --- which steps receive the date ----------------------------------------
def test_only_day_specific_steps_are_dated():
    dated = {label for label, _args, is_dated in rd.STEPS if is_dated}
    assert dated == {"attendance", "summary", "scorecard"}


def test_the_report_step_is_never_dated():
    """build_report clears each month tab and rewrites it from a rolling window,
    so a past date would delete the rows newer than that date."""
    assert dict((l, d) for l, _a, d in rd.STEPS)["report"] is False
    assert dict((l, d) for l, _a, d in rd.STEPS)["dashboard"] is False


def test_a_backfill_passes_the_date_only_to_dated_steps(monkeypatch, tmp_path):
    seen = child(tmp_path, "import sys; print('ARGS', ' '.join(sys.argv[1:]))\n")
    steps = [("dated", [str(seen)], True), ("undated", [str(seen)], False)]
    text = run(monkeypatch, tmp_path, steps, ["run_daily.py", _recent(1)])
    lines = [l.strip() for l in text.splitlines() if l.startswith("ARGS")]
    import datetime as _dt
    iso = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
    assert lines[0] == f"ARGS --date {iso}"    # dated step got it
    assert lines[1] == "ARGS"                  # undated step did not


def test_a_todays_run_passes_no_date_at_all(monkeypatch, tmp_path):
    """Unchanged behaviour when no date is given."""
    seen = child(tmp_path, "import sys; print('ARGS', ' '.join(sys.argv[1:]))\n")
    text = run(monkeypatch, tmp_path, [("dated", [str(seen)], True)])
    assert any(line.strip() == "ARGS" for line in text.splitlines())
    assert "--date" not in text


def test_a_backfill_is_marked_in_the_log(monkeypatch, tmp_path):
    seen = child(tmp_path, "print('ok')\n")
    text = run(monkeypatch, tmp_path, [("s", [str(seen)], False)], ["run_daily.py", _recent(2)])
    import datetime as _dt
    assert f"(backfill for {(_dt.date.today() - _dt.timedelta(days=2)).isoformat()})" in text
