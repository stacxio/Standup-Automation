"""Post a per-developer daily stand-up summary to Slack after the sheets update.

For each team member (the attendance roster) it reports three signals for TODAY:
  * stack-checkin -> 'done' if they posted a stand-up in Slack today, else
                     'No Update from Developer'.
  * jira          -> 'done' if any Jira issue they mentioned today has a comment
                     added today, else 'No update from Developer'.
  * On leave      -> 'applied' if attendance marks them Absent/Leave today,
                     else 'No'.

Reuses build_attendance (roster + check-ins + leaves) and build_report (today's
Jira ids), so the definitions stay identical to the two sheets. The summary is
posted as a separate message, in addition to the existing per-step notices.

Run:  .venv/Scripts/python.exe daily_summary.py            # print only (dry run)
      .venv/Scripts/python.exe daily_summary.py --notify   # also post to Slack
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

from standup_summarizer import jira  # noqa: E402
from standup_summarizer.config import JiraConfig, SlackConfig  # noqa: E402
from standup_summarizer.fetch import build_client  # noqa: E402

import build_attendance as att  # noqa: E402
import build_report as rep  # noqa: E402

# Status labels (kept close to the requested wording).
CHECKIN_DONE, CHECKIN_NONE = "done", "No Update from Developer"
JIRA_DONE, JIRA_NONE = "done", "No update from Developer"
LEAVE_YES, LEAVE_NO = "applied", "No"


def gather(cfg: SlackConfig, jira_cfg, today: dt.date) -> list[tuple[str, str, str, str]]:
    """Return [(developer, stack-checkin, jira, on-leave)] for the roster, today."""
    # Roster + attendance + who-checked-in, keyed by canonical short name.
    order, roll_calls, status_dates, leaves = att.fetch_channel(cfg)
    name_map = {s.lower().replace(" ", ""): s for s in order}

    # Today's Jira ids per developer: report uses full display names, so fold
    # them onto the same short names the attendance sheet uses.
    entries, _ = rep.fetch_standup_entries(cfg)
    tid = today.isoformat()
    picked: dict[str, set[str]] = {s: set() for s in order}
    for e in entries:
        if e["date"] != tid:
            continue
        short = att._short_of(e["developer"], name_map)
        picked.setdefault(short, set()).update(rep.extract_jira_ids(e["text"]))

    checked_in = status_dates.get(today, set())
    comment_cache: dict[str, bool | None] = {}
    rows: list[tuple[str, str, str, str]] = []
    for short in order:
        checkin = CHECKIN_DONE if short in checked_in else CHECKIN_NONE

        jira_line = JIRA_NONE
        if jira_cfg:
            for key in picked.get(short, set()):
                if key not in comment_cache:
                    comment_cache[key] = jira.issue_commented_on(jira_cfg, key, tid)
                if comment_cache[key]:
                    jira_line = JIRA_DONE
                    break

        cell = att.cell_value(short, today, roll_calls, leaves)
        leave_line = LEAVE_YES if cell in ("Absent", "Leave") else LEAVE_NO

        rows.append((short, checkin, jira_line, leave_line))
    return rows


def compose(rows: list[tuple[str, str, str, str]], today: dt.date) -> str:
    lines = [f"*Daily Stand-up Summary — {today.strftime('%d-%m-%Y')}*", ""]
    for short, checkin, jira_line, leave_line in rows:
        lines += [
            f"*{short}:*",
            f"stack-checkin: {checkin}",
            f"jira: {jira_line}",
            f"On leave: {leave_line}",
            "",
        ]
    return "\n".join(lines).rstrip()


def main() -> None:
    load_dotenv(ROOT / ".env")
    cfg = SlackConfig.from_env()
    jira_cfg = JiraConfig.from_env()
    if not jira_cfg:
        print("Jira not configured — the jira: line will read 'No update from Developer'.")

    today = dt.date.today()
    rows = gather(cfg, jira_cfg, today)
    text = compose(rows, today)
    print(text)

    if "--notify" in sys.argv or os.environ.get("NOTIFY_SLACK"):
        client = build_client(cfg.bot_token)
        try:
            resp = client.chat_postMessage(channel=cfg.channel_id, text=text)
            print(f"\nPosted daily summary (ts={resp.get('ts')}).")
        except Exception as exc:  # noqa: BLE001
            hint = ""
            if "missing_scope" in str(exc):
                hint = "  -> Add the 'chat:write' bot scope to the Slack app and reinstall it."
            print(f"\nSlack post failed: {exc}{hint}")


if __name__ == "__main__":
    main()
