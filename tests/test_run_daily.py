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


def run(monkeypatch, tmp_path: Path, steps: list[tuple[str, list[str]]]) -> str:
    """Run main() with `steps` and return what landed in the log."""
    log = tmp_path / "daily.log"
    monkeypatch.setattr(rd, "LOG", log)
    monkeypatch.setattr(rd, "PY", Path(sys.executable))
    monkeypatch.setattr(rd, "STEPS", steps)
    with pytest.raises(SystemExit) as exit_info:
        rd.main()
    run.code = exit_info.value.code
    return log.read_text(encoding="utf-8")


# --- log encoding ---------------------------------------------------------
def test_the_log_keeps_the_agents_unicode_intact(monkeypatch, tmp_path):
    """The regression: text=True without an encoding decoded UTF-8 as cp1252,
    so 'Scorecard — 2026-08-18' reached the log as 'Scorecard â€” 2026-08-18'."""
    script = child(tmp_path, f"print({UNICODE_LINE!r})\n")
    text = run(monkeypatch, tmp_path, [("emit", [str(script)])])
    assert UNICODE_LINE in text
    for mangled in ("â€", "Â·", "Ã¢"):
        assert mangled not in text


def test_agent_output_is_utf8_whatever_the_ambient_encoding(monkeypatch, tmp_path):
    """A stray PYTHONIOENCODING must not change what lands in the log."""
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    script = child(tmp_path, f"print({UNICODE_LINE!r})\n")
    assert UNICODE_LINE in run(monkeypatch, tmp_path, [("emit", [str(script)])])


def test_step_env_pins_the_child_encoding_without_dropping_the_environment():
    monkeypatch_free = rd.step_env()
    assert monkeypatch_free["PYTHONIOENCODING"] == "utf-8"
    assert "PATH" in monkeypatch_free      # inherits, does not replace


def test_undecodable_bytes_do_not_lose_the_step(monkeypatch, tmp_path):
    """errors='replace' — a stray byte must not cost us the whole step's output."""
    script = child(tmp_path, "import sys\n"
                             "sys.stdout.buffer.write(b'before \\xff\\xfe after\\n')\n")
    text = run(monkeypatch, tmp_path, [("emit", [str(script)])])
    assert "before" in text and "after" in text


# --- exit conventions -----------------------------------------------------
def test_exit_two_is_a_skip_not_a_failure(monkeypatch, tmp_path):
    script = child(tmp_path, "raise SystemExit(2)\n")
    run(monkeypatch, tmp_path, [("skipper", [str(script)])])
    assert run.code == 0


def test_a_failing_step_is_counted_and_logged(monkeypatch, tmp_path):
    script = child(tmp_path, "import sys; print('boom'); raise SystemExit(1)\n")
    text = run(monkeypatch, tmp_path, [("bad", [str(script)])])
    assert run.code == 1
    assert "--- bad (exit 1) ---" in text and "boom" in text


def test_one_failing_step_does_not_stop_the_rest(monkeypatch, tmp_path):
    bad = child(tmp_path, "raise SystemExit(1)\n", "bad.py")
    good = child(tmp_path, "print('ran anyway')\n", "good.py")
    text = run(monkeypatch, tmp_path, [("bad", [str(bad)]), ("good", [str(good)])])
    assert run.code == 1                      # the failure is still reported
    assert "ran anyway" in text               # ...but the later step still ran


def test_a_crashing_launch_is_caught_rather_than_aborting_the_run(monkeypatch, tmp_path):
    missing = tmp_path / "does_not_exist.py"
    good = child(tmp_path, "print('still ran')\n", "good.py")
    text = run(monkeypatch, tmp_path, [("gone", [str(missing)]), ("good", [str(good)])])
    assert "still ran" in text


def test_every_run_is_appended_under_a_dated_header(monkeypatch, tmp_path):
    script = child(tmp_path, "print('one')\n")
    run(monkeypatch, tmp_path, [("emit", [str(script)])])
    text = run(monkeypatch, tmp_path, [("emit", [str(script)])])
    assert text.count("daily run =====") == 2     # history accumulates
    assert text.count("one") == 2


# --- the pipeline itself --------------------------------------------------
def test_the_scorecard_and_dashboard_run_after_the_report():
    """Scoring reads the Jira state the report step has just refreshed."""
    labels = [label for label, _ in rd.STEPS]
    assert labels == ["attendance", "report", "summary", "scorecard", "dashboard"]


def test_the_scorecard_step_posts_and_the_dashboard_does_not():
    steps = dict(rd.STEPS)
    assert steps["scorecard"] == ["build_scorecard.py", "--score", "--notify"]
    assert "--notify" not in steps["dashboard"]


def test_every_step_names_a_script_that_exists():
    for _label, args in rd.STEPS:
        assert (ROOT / args[0]).exists(), args[0]
