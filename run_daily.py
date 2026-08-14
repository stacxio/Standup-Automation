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

Run:  .venv/Scripts/python.exe run_daily.py
"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"
LOG = ROOT / "logs" / "daily.log"

STEPS = [
    ("attendance", ["build_attendance.py", "--notify"]),
    ("report", ["build_report.py", "--notify"]),
    # The summary posts on every run — no --notify needed (it is still accepted).
    ("summary", ["daily_summary.py"]),
    # Scoring runs last: it reads the Jira state the report step has just
    # refreshed, and posts every developer's score to the check-in channel.
    ("scorecard", ["build_scorecard.py", "--score", "--notify"]),
    ("dashboard", ["build_dashboard.py"]),
]


def main() -> None:
    LOG.parent.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    failures = 0
    with LOG.open("a", encoding="utf-8") as log:
        log.write(f"\n===== {stamp} daily run =====\n")
        for label, args in STEPS:
            try:
                r = subprocess.run(
                    [str(PY), *args], cwd=str(ROOT),
                    capture_output=True, text=True, timeout=600,
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
