"""Post a per-developer daily stand-up digest to Slack after the sheets update.

For every team member (the attendance roster) it reports, for TODAY:

    *Kavin*

    checkin:
    status: done                        <- posted a stand-up in the check-in
                                           channel today, else "No update from
                                           developer"
    Previous task: ST-11                <- task id(s) completed on the most
                                           recent earlier day, else "no update"
    Task picked: ST-11, ST-22           <- Jira ids mentioned in today's post
    Task done: ST-11                    <- of those, the ones Jira marks done

    jira:
    ST-11: <what the task is about>     <- the Jira summary line
    description: yes                    <- description field filled in?
    branch: feature/something           <- from the Jira "Development" panel
    commit: 12299y7
    PR: https://github.com/.../pull/4
    PR merged: no

    Comments:
    ST-11: no comments                  <- did the developer comment on Jira?

Reuses build_attendance (roster + check-ins) and build_report (Jira ids per
stand-up), so the definitions stay identical to the two sheets. The digest is
posted to the check-in channel (SLACK_CHANNEL_ID — #stacx-check-in) on every
run, as a separate message from the existing per-step notices. Long digests are
split into several messages on developer boundaries.

Run:  .venv/Scripts/python.exe daily_summary.py             # post to Slack
      .venv/Scripts/python.exe daily_summary.py --dry-run   # print only
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
CHECKIN_DONE, CHECKIN_NONE = "done", "No update from developer"
NO_UPDATE = "no update"
NOT_LINKED = "not linked"
NO_COMMENTS = "no comments"
UNKNOWN = "unknown"
NO_TASKS = "no task picked today"
NO_JIRA_CFG = "Jira not configured."

# Slack rejects very long messages; the digest is split on developer blocks.
SLACK_CHUNK_CHARS = 3500


# --------------------------------------------------------------------------
# Jira lookups (cached — each issue key is fetched at most once per run)
# --------------------------------------------------------------------------
class _JiraLookup:
    """Per-run cache around the Jira reads the digest needs."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._detail: dict[str, dict | None] = {}
        self._dev: dict[str, dict] = {}
        self._comments: dict[str, list[dict] | None] = {}

    def detail(self, key: str) -> dict | None:
        if key not in self._detail:
            self._detail[key] = jira.issue_detail(self.cfg, key) if self.cfg else None
        return self._detail[key]

    def is_done(self, key: str) -> bool:
        d = self.detail(key)
        return bool(d and d["status_category"] == "done")

    def dev_info(self, key: str) -> dict:
        if key not in self._dev:
            d = self.detail(key)
            self._dev[key] = (
                jira.issue_dev_info(self.cfg, d["id"]) if (self.cfg and d) else
                {"branches": [], "commits": [], "pull_requests": []}
            )
        return self._dev[key]

    def comments(self, key: str) -> list[dict] | None:
        if key not in self._comments:
            self._comments[key] = jira.issue_comments(self.cfg, key) if self.cfg else None
        return self._comments[key]


def _norm(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _same_person(author: str, short: str) -> bool:
    """Loose match between a Jira comment author and a roster short name."""
    a, s = _norm(author), _norm(short)
    if not a or not s:
        return False
    return a.startswith(s) or s in a or a in s


# --------------------------------------------------------------------------
# Per-issue rendering
# --------------------------------------------------------------------------
def issue_block(key: str, look: _JiraLookup) -> list[str]:
    """The 'jira:' lines for one issue key."""
    detail = look.detail(key)
    if detail is None:
        return [f"{key}: {UNKNOWN} (could not read the issue from Jira)"]

    dev = look.dev_info(key)
    branches = dev["branches"]
    commits = dev["commits"]
    prs = dev["pull_requests"]

    commit = NOT_LINKED
    if commits:
        commit = commits[0]
        if len(commits) > 1:
            commit += f" (+{len(commits) - 1} more)"

    pr_urls = [p["url"] or p["name"] or p["id"] for p in prs if (p["url"] or p["name"] or p["id"])]
    merged = "yes" if any(p["status"] == "MERGED" for p in prs) else "no"

    return [
        f"{key}: {detail['summary'] or UNKNOWN}",
        f"description: {'yes' if detail['description'] else 'no'}",
        f"branch: {', '.join(branches) if branches else NOT_LINKED}",
        f"commit: {commit}",
        f"PR: {', '.join(pr_urls) if pr_urls else NOT_LINKED}",
        f"PR merged: {merged if prs else 'no'}",
    ]


def comment_line(key: str, short: str, look: _JiraLookup) -> str:
    """The 'Comments:' line for one issue key."""
    comments = look.comments(key)
    if comments is None:
        return f"{key}: {UNKNOWN}"
    if not comments:
        return f"{key}: {NO_COMMENTS}"
    if any(_same_person(c["author"], short) for c in comments):
        return f"{key}: yes"
    return f"{key}: yes (by others)"


# --------------------------------------------------------------------------
# Gather
# --------------------------------------------------------------------------
def _previous_done(days: dict[str, list[str]], today_iso: str, look: _JiraLookup) -> list[str]:
    """Jira ids completed on the most recent day before today; [] if none."""
    for day in sorted((d for d in days if d < today_iso), reverse=True):
        done = [k for k in days[day] if look.is_done(k)]
        if done:
            return done
    return []


def gather(cfg: SlackConfig, jira_cfg, today: dt.date) -> list[dict]:
    """Return one digest record per roster member, for `today`."""
    order, _roll_calls, status_dates, _leaves = att.fetch_channel(cfg)
    name_map = {s.lower().replace(" ", ""): s for s in order}

    # Jira ids per developer per date. The report uses full display names, so
    # fold them onto the same short names the attendance sheet uses.
    entries, _ = rep.fetch_standup_entries(cfg)
    by_dev: dict[str, dict[str, list[str]]] = {s: {} for s in order}
    for e in entries:
        short = att._short_of(e["developer"], name_map)
        day = by_dev.setdefault(short, {}).setdefault(e["date"], [])
        for key in rep.extract_jira_ids(e["text"]):
            if key not in day:
                day.append(key)

    tid = today.isoformat()
    checked_in = status_dates.get(today, set())
    look = _JiraLookup(jira_cfg)

    rows: list[dict] = []
    for short in order:
        days = by_dev.get(short, {})
        picked = days.get(tid, [])
        done = [k for k in picked if look.is_done(k)] if jira_cfg else []
        previous = _previous_done(days, tid, look) if jira_cfg else []

        rows.append({
            "name": short,
            "checkin": CHECKIN_DONE if short in checked_in else CHECKIN_NONE,
            "previous": previous,
            "picked": picked,
            "done": done,
            "jira": [issue_block(k, look) for k in picked] if jira_cfg else [],
            "comments": [comment_line(k, short, look) for k in picked] if jira_cfg else [],
        })
    return rows


# --------------------------------------------------------------------------
# Compose
# --------------------------------------------------------------------------
def _join(ids: list[str]) -> str:
    return ", ".join(ids) if ids else NO_UPDATE


def developer_block(row: dict, jira_configured: bool) -> str:
    lines = [
        f"*{row['name']}*",
        "",
        "checkin:",
        f"status: {row['checkin']}",
        f"Previous task: {_join(row['previous'])}",
        f"Task picked: {_join(row['picked'])}",
        f"Task done: {_join(row['done'])}",
        "",
        "jira:",
    ]
    if not jira_configured:
        lines.append(NO_JIRA_CFG)
    elif not row["jira"]:
        lines.append(NO_TASKS)
    else:
        for i, block in enumerate(row["jira"]):
            if i:
                lines.append("")
            lines += block

    lines += ["", "Comments:"]
    if not jira_configured:
        lines.append(NO_JIRA_CFG)
    elif not row["comments"]:
        lines.append(NO_COMMENTS)
    else:
        lines += row["comments"]

    return "\n".join(lines)


def compose_blocks(rows: list[dict], today: dt.date, jira_configured: bool) -> list[str]:
    """Return [header, developer block, ...] — the digest, one entry per person."""
    header = f"*Daily Stand-up Summary — {today.strftime('%d-%m-%Y')}*"
    return [header] + [developer_block(r, jira_configured) for r in rows]


def compose(rows: list[dict], today: dt.date, jira_configured: bool = True) -> str:
    return "\n\n".join(compose_blocks(rows, today, jira_configured))


def chunk(blocks: list[str], limit: int = SLACK_CHUNK_CHARS) -> list[str]:
    """Pack whole blocks into messages of at most `limit` chars (never splits one)."""
    messages: list[str] = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if current and len(candidate) > limit:
            messages.append(current)
            current = block
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


# --------------------------------------------------------------------------
def post(cfg: SlackConfig, blocks: list[str]) -> None:
    """Post the digest to the check-in channel (SLACK_CHANNEL_ID)."""
    client = build_client(cfg.bot_token)
    for i, text in enumerate(chunk(blocks), start=1):
        try:
            resp = client.chat_postMessage(channel=cfg.channel_id, text=text)
            print(f"\nPosted daily summary part {i} (ts={resp.get('ts')}).")
        except Exception as exc:  # noqa: BLE001
            hint = ""
            if "missing_scope" in str(exc):
                hint = "  -> Add the 'chat:write' bot scope to the Slack app and reinstall it."
            print(f"\nSlack post failed: {exc}{hint}")
            break


def main() -> None:
    load_dotenv(ROOT / ".env")
    cfg = SlackConfig.from_env()
    jira_cfg = JiraConfig.from_env()
    if not jira_cfg:
        print("Jira not configured — the jira: and Comments: sections will be empty "
              "(set JIRA_BASE_URL/JIRA_EMAIL/JIRA_API_TOKEN in .env to enable).")

    today = dt.date.today()
    rows = gather(cfg, jira_cfg, today)
    blocks = compose_blocks(rows, today, bool(jira_cfg))
    print("\n\n".join(blocks))

    # The digest posts on every run. --dry-run (or DRY_RUN=1) prints only, for
    # testing. --notify is still accepted so older callers keep working.
    if "--dry-run" in sys.argv or "--no-notify" in sys.argv or os.environ.get("DRY_RUN"):
        print("\nDry run — not posting to Slack.")
        return
    post(cfg, blocks)


if __name__ == "__main__":
    main()
