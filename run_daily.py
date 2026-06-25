"""Daily job: refresh attendance (+ Slack notify) and the Daily Status report.

Runs build_attendance.py (--notify) then build_report.py as subprocesses and
appends their combined output to logs/daily.log. This is what the Windows
scheduled task invokes.

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
    ("report", ["build_report.py"]),
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
                print(f"{label}: exit {r.returncode}")
                if r.returncode != 0:
                    failures += 1
            except Exception as exc:  # noqa: BLE001
                log.write(f"--- {label} FAILED: {exc} ---\n")
                print(f"{label}: FAILED ({exc})")
                failures += 1
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
