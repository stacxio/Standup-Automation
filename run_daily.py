"""Daily job: refresh attendance, the Daily Status report, summary and scores.

Runs each agent as a subprocess and appends the combined output to
logs/daily.log. This is what the Windows scheduled task invokes.

The scorecard step posts every developer's score for the day to the check-in
channel (SCORE_PUBLIC_SCORES=true) and refreshes the dashboard. Two scorecard
jobs are *not* here and need their own scheduled tasks, because this run happens
after the workday:
  * `build_scorecard.py --capture`  at ~11:00 — freezes the day's commitments
    before the workday closes. Without it the 18:00 run captures late, which
    still works but cannot catch a task quietly dropped during the day.
  * `build_scorecard.py --recompute --days 3` at ~02:00 — the self-correcting
    pass that picks up late Jira updates.

Setup reminder (config lives in .env, which is gitignored — not in the repo):
  * The summary step routes each project's slice to its own Slack channel via
    SUMMARY_CHANNEL_ROUTES (prefix:channel_id, e.g. "SP:C…,WS:C…,HIR:C…,BHA:C…").
    Unrouted prefixes and task-less developers fall back to SLACK_CHANNEL_ID.
  * The bot must be invited (/invite) into every routed channel — it has
    chat:write but not channels:join, so it cannot add itself. A channel it is
    not in is skipped with a not_in_channel notice; the other channels still post.
  * Current routing: SP/WS -> #feedback-stacx, HIR -> #feedback-hirocom,
    BHA -> #feedback-bha. See .env.example / README for the full contract.

Run:  .venv/Scripts/python.exe run_daily.py                  # today
      .venv/Scripts/python.exe run_daily.py 19-08-2026      # backfill that day
      .venv/Scripts/python.exe run_daily.py --date 2026-08-19

A backfill passes the date to the steps that are about one particular day, and
posts to Slack for that date — the digest header and the scorecard both name it,
so a message arriving today cannot be mistaken for today's. The report and
dashboard steps rebuild a rolling window and the whole fact table, so they run
undated whatever the run date is.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

from standup_summarizer.dates import day_from_argv, parse_day  # noqa: E402
PY = ROOT / ".venv" / "Scripts" / "python.exe"
LOG = ROOT / "logs" / "daily.log"

# dated=True: the step is about one particular day and takes --date.
# dated=False: the step rebuilds a rolling window or the whole history, so a
# date would be meaningless — or worse. build_report clears each month tab and
# rewrites it from its 30-day window, so pointing it at a past date would
# silently delete the rows newer than that date.
STEPS = [
    ("attendance", ["build_attendance.py", "--notify"], True),
    ("report", ["build_report.py", "--notify"], False),
    # The summary posts on every run — no --notify needed (it is still accepted).
    ("summary", ["daily_summary.py"], True),
    # Scoring runs last: it reads the Jira state the report step has just
    # refreshed, and posts every developer's score to the check-in channel.
    ("scorecard", ["build_scorecard.py", "--score", "--notify"], True),
    ("dashboard", ["build_dashboard.py"], False),
]

# Furthest back a step can see: build_attendance reads 120 days of Slack
# history, build_report 30. Past that the history is simply not fetched and a
# backfill would look successful while producing nothing.
LOOKBACK_DAYS = 120


def step_env() -> dict:
    """Environment for the agents, pinning their output to UTF-8.

    Whether a piped child emits UTF-8 or the Windows ANSI codepage otherwise
    depends on ambient settings (PYTHONUTF8 / PYTHONIOENCODING / the console
    codepage), which is not something an unattended weekday job should vary on.
    Pinning it here means the bytes written to the log are the bytes we decode.
    """
    return {**os.environ, "PYTHONIOENCODING": "utf-8"}


def resolve_day(argv: list[str]) -> dt.date:
    """The day to run for: `--date 19-08-2026`, the same value positionally, or today.

    Refuses a future date, and refuses one older than the Slack history the
    steps can reach — a backfill that quietly finds no data is worse than one
    that stops and says so.
    """
    candidates = [a for a in argv[1:] if not a.startswith("-")]
    try:
        day = day_from_argv(argv, default=parse_day(candidates[0]) if candidates else None)
    except ValueError as exc:
        raise SystemExit(str(exc))

    today = dt.date.today()
    if day > today:
        raise SystemExit(f"{day.strftime('%d-%m-%Y')} is in the future — nothing to run.")
    if (today - day).days > LOOKBACK_DAYS:
        raise SystemExit(
            f"{day.strftime('%d-%m-%Y')} is {(today - day).days} days ago, beyond the "
            f"{LOOKBACK_DAYS}-day Slack history the steps read. The run would find "
            f"nothing and still look successful."
        )
    return day


def main(argv: list[str] | None = None) -> None:
    day = resolve_day(sys.argv if argv is None else argv)
    backfill = day != dt.date.today()

    LOG.parent.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    failures = 0
    with LOG.open("a", encoding="utf-8") as log:
        log.write(f"\n===== {stamp} daily run"
                  f"{' (backfill for ' + day.isoformat() + ')' if backfill else ''} =====\n")
        if backfill:
            print(f"Backfilling {day.strftime('%d-%m-%Y')}. "
                  f"Slack posts will name that date.")
        for label, args, dated in STEPS:
            if backfill and dated:
                args = [*args, "--date", day.isoformat()]
            try:
                r = subprocess.run(
                    [str(PY), *args], cwd=str(ROOT),
                    capture_output=True, text=True, timeout=600,
                    # The agents print UTF-8 (em-dashes, "·", developer names).
                    # Without an explicit encoding, text=True decodes using the
                    # Windows ANSI codepage and mangles them in the log — which
                    # is the one place you look after an unattended failure.
                    # errors="replace" keeps a stray byte from raising here and
                    # losing the whole step's output.
                    encoding="utf-8", errors="replace", env=step_env(),
                )
                log.write(f"--- {label} (exit {r.returncode}) ---\n")
                log.write((r.stdout or "") + (r.stderr or "") + "\n")
                # Convention: exit code 2 = the step skipped itself (e.g. report
                # with no reasoning backend configured). Not counted as a failure.
                if r.returncode == 2:
                    print(f"{label}: skipped (exit 2)")
                elif r.returncode != 0:
                    print(f"{label}: FAILED (exit {r.returncode})")
                    failures += 1
                else:
                    print(f"{label}: ok")
            except Exception as exc:  # noqa: BLE001
                log.write(f"--- {label} FAILED: {exc} ---\n")
                print(f"{label}: FAILED ({exc})")
                failures += 1
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
