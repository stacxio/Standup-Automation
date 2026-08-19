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
stand-up), so the definitions stay identical to the two sheets.

Routing: each developer's tasks are split by issue-key prefix and posted to the
matching project channel (SUMMARY_CHANNEL_ROUTES, e.g. "SP:C…,WS:C…,HIR:C…").
A prefix with no route — and any developer who referenced no task — falls back
to the default channel (SLACK_CHANNEL_ID, #stacx-check-in). Long slices are
split into several messages on developer boundaries.

  SUMMARY_CHANNEL_ROUTES=SP:C0AAA,WS:C0AAA,HIR:C0BBB,BHA:C0CCC

The bot must be a member of every target channel (else Slack returns
'not_in_channel'); a failure on one channel never stops the others.

Run:  .venv/Scripts/python.exe daily_summary.py             # post to Slack
      .venv/Scripts/python.exe daily_summary.py --dry-run   # print only
"""

from __future__ import annotations

import datetime as dt
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

from standup_summarizer import jira, scorecard  # noqa: E402
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
FROM_COMMENTS = "(from comments)"
# The commit line answers "is there a commit for this task, yes or no" — the id
# itself is in the scorecard's evidence column for anyone who needs to trace it.
LINKED = "Yes"

# A pull request named in prose: GitHub /pull/N, Bitbucket /pull-requests/N,
# GitLab /merge_requests/N. Only a real URL counts — "raised the PR" is not one.
_PR_URL = re.compile(r"https?://\S+?/(?:pull|pull-requests|merge_requests)/\d+", re.I)

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
def _scm_from_comments(key: str, look: _JiraLookup) -> tuple[list[str], list[str]]:
    """Commit ids and PR urls named in the issue's comments -> (commits, prs).

    Jira's development panel is populated only by a real GitHub/Bitbucket/GitLab
    integration; text typed into a comment never reaches it. With no SCM linked
    to this Jira the panel is empty on every issue, while the team's habit is to
    paste the branch, commit and PR into a comment — so "not linked" was being
    reported over work that plainly exists.

    Reads the same commit-id detector the scorecard uses, so the digest and the
    score cannot disagree about whether an issue has a commit.
    """
    commits: list[str] = []
    prs: list[str] = []
    for comment in (look.comments(key) or []):
        body = comment.get("body", "")
        commits += [c for c in scorecard.commit_ids_in_text(body) if c not in commits]
        prs += [u for u in _PR_URL.findall(body) if u not in prs]
    return commits, prs


def issue_block(key: str, look: _JiraLookup) -> list[str]:
    """The 'jira:' lines for one issue key."""
    detail = look.detail(key)
    if detail is None:
        return [f"{key}: {UNKNOWN} (could not read the issue from Jira)"]

    dev = look.dev_info(key)
    branches = dev["branches"]
    commits = dev["commits"]
    prs = dev["pull_requests"]

    pr_urls = [p["url"] or p["name"] or p["id"] for p in prs if (p["url"] or p["name"] or p["id"])]
    merged = "yes" if any(p["status"] == "MERGED" for p in prs) else "no"

    # The panel is authoritative; fall back per field so a partially linked
    # issue keeps the real data and only the gaps come from prose.
    commit_note, pr_note = "", ""
    if not commits or not pr_urls:
        found_commits, found_prs = _scm_from_comments(key, look)
        if not commits and found_commits:
            commits, commit_note = found_commits, f" {FROM_COMMENTS}"
        if not pr_urls and found_prs:
            pr_urls, pr_note = found_prs, f" {FROM_COMMENTS}"
            # Prose cannot tell us whether a PR merged; "no" would be a claim.
            merged = UNKNOWN

    commit = f"{LINKED}{commit_note}" if commits else NOT_LINKED

    return [
        f"{key}: {detail['summary'] or UNKNOWN}",
        f"description: {'yes' if detail['description'] else 'no'}",
        # Branch is not derived from prose: a branch name in a sentence has no
        # reliable shape ("Latest branch: main GitHub: ..."), and guessing one
        # would put a wrong name in front of the team.
        f"branch: {', '.join(branches) if branches else NOT_LINKED}",
        f"commit: {commit}",
        f"PR: {', '.join(pr_urls) + pr_note if pr_urls else NOT_LINKED}",
        f"PR merged: {merged if (prs or pr_urls) else 'no'}",
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
# Routing — send each developer's task slice to its project's channel
# --------------------------------------------------------------------------
def prefix_of(key: str) -> str:
    """The project key of an issue id, upper-cased: 'HIR-72' -> 'HIR'."""
    return key.split("-", 1)[0].upper() if "-" in key else key.upper()


def _channel_of(cfg: SlackConfig, key: str) -> str:
    return cfg.channel_for_prefix(prefix_of(key))


def _filter_row(row: dict, channel: str, cfg: SlackConfig) -> dict:
    """A copy of `row` keeping only the ids whose prefix routes to `channel`.

    picked/jira/comments stay index-aligned; previous and done are filtered by
    the same channel rule.
    """
    keep = [i for i, k in enumerate(row["picked"]) if _channel_of(cfg, k) == channel]
    return {
        **row,
        "previous": [k for k in row["previous"] if _channel_of(cfg, k) == channel],
        "picked": [row["picked"][i] for i in keep],
        "done": [k for k in row["done"] if _channel_of(cfg, k) == channel],
        "jira": [row["jira"][i] for i in keep] if row["jira"] else [],
        "comments": [row["comments"][i] for i in keep] if row["comments"] else [],
    }


def route_blocks(rows: list[dict], cfg: SlackConfig, today: dt.date,
                 jira_configured: bool) -> dict[str, list[str]]:
    """Group developer blocks by target channel.

    Each developer's tasks are split by issue-key prefix; the slice for each
    project posts to that project's channel (SUMMARY_CHANNEL_ROUTES), with
    unrouted prefixes and task-less developers falling back to the default
    channel (SLACK_CHANNEL_ID). Returns {channel: [header, block, ...]}.
    """
    header = f"*Daily Stand-up Summary — {today.strftime('%d-%m-%Y')}*"
    per_channel: dict[str, list[str]] = {}

    def add(channel: str, block: str) -> None:
        per_channel.setdefault(channel, []).append(block)

    for row in rows:
        ids = row["picked"] + row["previous"] + row["done"]
        if not ids:  # nothing routable — keep the check-in signal in the default
            add(cfg.channel_id, developer_block(row, jira_configured))
            continue
        channels: list[str] = []
        for k in ids:
            c = _channel_of(cfg, k)
            if c not in channels:
                channels.append(c)
        for channel in channels:
            add(channel, developer_block(_filter_row(row, channel, cfg), jira_configured))

    return {channel: [header] + blocks for channel, blocks in per_channel.items()}


def _channel_label(cfg: SlackConfig, channel: str) -> str:
    """Human hint for a channel id in dry-run output: which prefixes route to it."""
    prefixes = sorted(p for p, c in cfg.channel_routes.items() if c == channel)
    tag = "default / SLACK_CHANNEL_ID" if channel == cfg.channel_id else "routed"
    joined = ", ".join(prefixes) if prefixes else "—"
    return f"{channel}  ({tag}; prefixes: {joined})"


# --------------------------------------------------------------------------
def post(cfg: SlackConfig, routed: dict[str, list[str]]) -> None:
    """Post each channel's blocks; a failure on one channel never stops the rest."""
    client = build_client(cfg.bot_token)
    for channel, blocks in routed.items():
        for i, text in enumerate(chunk(blocks), start=1):
            try:
                resp = client.chat_postMessage(channel=channel, text=text)
                print(f"Posted to {channel} part {i} (ts={resp.get('ts')}).")
            except Exception as exc:  # noqa: BLE001
                hint = ""
                if "missing_scope" in str(exc):
                    hint = "  -> Add the 'chat:write' bot scope to the Slack app and reinstall it."
                elif "not_in_channel" in str(exc):
                    hint = f"  -> Invite the bot into {channel} (/invite @<bot>)."
                elif "channel_not_found" in str(exc):
                    hint = f"  -> Check the channel id for {channel} in SUMMARY_CHANNEL_ROUTES."
                print(f"Slack post to {channel} failed: {exc}{hint}")
                break  # skip the rest of THIS channel; continue with the others


def main() -> None:
    load_dotenv(ROOT / ".env")
    cfg = SlackConfig.from_env()
    jira_cfg = JiraConfig.from_env()
    if not jira_cfg:
        print("Jira not configured — the jira: and Comments: sections will be empty "
              "(set JIRA_BASE_URL/JIRA_EMAIL/JIRA_API_TOKEN in .env to enable).")

    today = dt.date.today()
    rows = gather(cfg, jira_cfg, today)
    routed = route_blocks(rows, cfg, today, bool(jira_cfg))

    # Preview: show each channel and the slice bound for it.
    for channel, blocks in routed.items():
        print(f"\n{'=' * 70}\n>>> {_channel_label(cfg, channel)}\n{'=' * 70}")
        print("\n\n".join(blocks))

    # The digest posts on every run. --dry-run (or DRY_RUN=1) prints only, for
    # testing. --no-notify is accepted as an alias.
    if "--dry-run" in sys.argv or "--no-notify" in sys.argv or os.environ.get("DRY_RUN"):
        print("\nDry run — not posting to Slack.")
        return
    print()
    post(cfg, routed)


if __name__ == "__main__":
    main()
