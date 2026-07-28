"""Daily job: refresh attendance, the Daily Status report, and post a summary.

Runs build_attendance.py, build_report.py, then daily_summary.py as subprocesses
and appends their combined output to logs/daily.log. This is what the Windows
scheduled task invokes.

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
