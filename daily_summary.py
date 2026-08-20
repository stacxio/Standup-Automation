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
from standup_summarizer.dates import day_from_argv  # noqa: E402
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

# Commit ids are shown verbatim — exactly the id the developer recorded, not a
# shortened or reformatted one, so it can be pasted straight into `git show`.
# One issue in this Jira carries 19 commits in a single comment, so the line is
# capped; the count of the remainder still tells you they exist.
MAX_COMMITS_SHOWN = 3

# --------------------------------------------------------------------------
# Reading SCM facts out of comment prose.
#
# TEMPORARY. This exists only because no SCM is connected to this Jira, so the
# development panel is empty on every issue and the team records the branch,
# commit and PR by typing them into a comment. Connect GitHub to Jira and the
# panel becomes authoritative again — the panel is always preferred below, so
# this whole path simply stops being reached. Prose is a weaker source and is
# always labelled as such in the digest.
# --------------------------------------------------------------------------

# A pull request named in prose: GitHub /pull/N, Bitbucket /pull-requests/N,
# GitLab /merge_requests/N.
_PR_URL = re.compile(r"https?://\S+?/(?:pull|pull-requests|merge_requests)/\d+", re.I)
# ...or a labelled one: "PR: #42", "Pull request - https://…", "PR link: …".
_PR_LABEL = re.compile(r"\b(?:pull\s*requests?|prs?)\b\s*(?:link|url|id|no\.?)?\s*[:\-–]\s*(\S+)", re.I)
# A branch is a single token — git forbids spaces — so capture one token after
# the label. That is what makes "Latest branch: main GitHub: agb-admin:" yield
# "main" rather than swallowing the labels that follow it.
#
# The trailing (?![\w./-:]) is load-bearing. Developers post the template with
# the fields left blank — "branch: commit: PR: not raised yet" — and without it
# the branch reads as "commit", i.e. the *next label* becomes the value. A token
# followed by a colon is a label, never a value, and refusing to backtrack
# inside the token class stops "commit:" degrading to "commi".
_BRANCH_LABEL = re.compile(
    r"(?:latest\s+)?branch(?:\s*name)?\s*[:\-–]\s*([\w./-]+)(?![\w./-:])", re.I
)
# Words developers write where a value would go; not branch names or PRs.
_PLACEHOLDERS = {"na", "n/a", "none", "nil", "no", "yes", "tbd", "pending", "raised",
                 "done", "created", "wip", "-", "--", "nan", "null", "todo",
                 "not", "yet", "branch", "commit", "pr", "url", "link"}

# "PR merged: yes" / "PR merged - no PR exists" / "Pull request merged : Y".
_MERGED_LABEL = re.compile(
    r"\b(?:pr|pull\s*requests?)\s*(?:is\s*)?merged\b\s*[:\-–]?\s*([^\n]{0,40})", re.I
)
_MERGED_YES = {"yes", "y", "merged", "done", "true", "completed", "complete"}
_MERGED_NO = {"no", "n", "not", "false", "pending", "yet", "nope", "na", "n/a"}
# Shown when the developer has said nothing about whether the PR merged.
MERGED_NO_UPDATE = "No update"

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
def _is_value(token: str) -> bool:
    """Whether a captured token is a real value rather than a placeholder word."""
    return bool(token) and token.strip().lower().strip(".,;") not in _PLACEHOLDERS


def _merged_from_text(value: str) -> str | None:
    """Read "PR merged: ..." as Yes / No, or None when it says nothing useful.

    Classified on the first word, so "no PR exists" reads as No and "Merged on
    the 12th" reads as Yes. An empty value ("PR merged:" with nothing after it,
    which is how the blank template arrives) yields None, not a guess.
    """
    words = value.strip().lower().strip(":-–").split()
    if not words:
        return None
    first = words[0].strip(".,;:")
    if first in _MERGED_YES:
        return "Yes"
    if first in _MERGED_NO:
        return "No"
    return None


def _is_pr_ref(token: str) -> bool:
    """A pull request is a url or a number — "not raised yet" is neither.

    Without this, a blank template line ("PR: not raised yet") reports "not" as
    the pull request.
    """
    value = token.strip().rstrip(".,;")
    return value.lower().startswith("http") or any(ch.isdigit() for ch in value)


def _scm_from_comments(key: str, look: _JiraLookup) -> dict[str, list[str]]:
    """Branch, commit and PR named in the issue's comments.

    Jira's development panel is populated only by a real GitHub/Bitbucket/GitLab
    integration; text typed into a comment never reaches it. With no SCM linked
    to this Jira the panel is empty on every issue, while the team's habit is to
    paste all three into a comment — so "not linked" was being reported over
    work that plainly exists.

    Commits reuse the scorecard's detector, so the digest and the score cannot
    disagree about whether an issue has one. Branch and PR are digest-only:
    nothing scores them, so a looser read costs nobody points.
    """
    found: dict = {"branches": [], "commits": [], "pull_requests": [], "merged": None}

    def add(field: str, value: str, *, fold_case: bool = False) -> None:
        value = value.strip().rstrip(".,;")
        if not _is_value(value):
            return
        # "Latest branch: main" and "Live Branch Name : Main" are one branch
        # written twice, so branches dedupe case-insensitively; commit ids and
        # urls are compared exactly.
        seen = [v.lower() for v in found[field]] if fold_case else found[field]
        if (value.lower() if fold_case else value) not in seen:
            found[field].append(value)

    for comment in (look.comments(key) or []):
        body = comment.get("body", "")
        for commit in scorecard.commit_ids_in_text(body):
            add("commits", commit)
        for url in _PR_URL.findall(body):
            add("pull_requests", url)
        for token in _PR_LABEL.findall(body):
            if _is_pr_ref(token):
                add("pull_requests", token)
        for branch in _BRANCH_LABEL.findall(body):
            add("branches", branch, fold_case=True)
        # The newest statement wins: comments are returned newest-first, so the
        # first one that says anything is the developer's latest word on it.
        if found["merged"] is None:
            for stated in _MERGED_LABEL.findall(body):
                verdict = _merged_from_text(stated)
                if verdict:
                    found["merged"] = verdict
                    break
    return found


def _render_ids(ids: list[str]) -> str:
    """Commit ids for the digest line, verbatim and capped.

    Shown exactly as recorded — not shortened — so an id can be pasted straight
    into `git show`. Beyond MAX_COMMITS_SHOWN the remainder is counted rather
    than listed, because a single comment here can carry nineteen of them and
    Slack rejects very long messages.
    """
    shown = ", ".join(ids[:MAX_COMMITS_SHOWN])
    extra = len(ids) - MAX_COMMITS_SHOWN
    return f"{shown} (+{extra} more)" if extra > 0 else shown


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

    # The panel is authoritative; fall back per field so a partially linked
    # issue keeps its real data and only the gaps come from prose.
    notes = {"branches": "", "commits": "", "pull_requests": ""}
    merged = "Yes" if any(p["status"] == "MERGED" for p in prs) else None
    if not (branches and commits and pr_urls and prs):
        found = _scm_from_comments(key, look)
        if not branches and found["branches"]:
            branches, notes["branches"] = found["branches"], f" {FROM_COMMENTS}"
        if not commits and found["commits"]:
            commits, notes["commits"] = found["commits"], f" {FROM_COMMENTS}"
        if not pr_urls and found["pull_requests"]:
            pr_urls, notes["pull_requests"] = found["pull_requests"], f" {FROM_COMMENTS}"
        if merged is None:
            # Whatever the developer said, or nothing if they said nothing —
            # "no" would claim the PR did not merge when we simply do not know.
            merged = found["merged"] or ("No" if prs else None)

    def line(label: str, values: list[str], field: str) -> str:
        return (f"{label}: {_render_ids(values)}{notes[field]}" if values
                else f"{label}: {NOT_LINKED}")

    return [
        f"{key}: {detail['summary'] or UNKNOWN}",
        f"description: {'yes' if detail['description'] else 'no'}",
        line("branch", branches, "branches"),
        line("commit", commits, "commits"),
        line("PR", pr_urls, "pull_requests"),
        f"PR merged: {merged or MERGED_NO_UPDATE}",
    ]


def status_entry(key: str, look: _JiraLookup) -> str:
    """One issue's live Jira status, as "HIR-91-In Progress".

    The status name is taken verbatim from Jira — "Review", not a normalised
    "In Review" — so the digest says exactly what the board says.
    """
    detail = look.detail(key)
    return f"{key}-{(detail or {}).get('status_name') or UNKNOWN}"


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
def _previous_tasks(days: dict[str, list[str]], today_iso: str) -> list[str]:
    """The Jira ids from the developer's most recent stand-up before today.

    Whatever they last said they were working on, regardless of where those
    tickets have got to. This previously reported only the ids Jira considered
    *done*, which meant a developer who had carried the same unfinished ticket
    for days showed "no update" — precisely the case where seeing yesterday's
    ticket matters most.

    Days the developer posted no ticket id are skipped, so this walks back to
    their last stand-up that named one. Needs no Jira lookup, so it works even
    when Jira is unreachable.
    """
    for day in sorted((d for d in days if d < today_iso), reverse=True):
        if days[day]:
            return days[day]
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
        previous = _previous_tasks(days, tid)

        rows.append({
            "name": short,
            "checkin": CHECKIN_DONE if short in checked_in else CHECKIN_NONE,
            "previous": previous,
            "picked": picked,
            "done": done,
            # Index-aligned with `picked`, so per-channel routing filters it too.
            "task_status": [status_entry(k, look) for k in picked] if jira_cfg else [],
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
        f"Task status: {_join(row.get('task_status') or [])}",
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


def _row_slice(row: dict, indices: list[int], *, first: bool) -> dict:
    """A copy of `row` carrying only `indices` of its tasks.

    The per-task lists are index-aligned, exactly as `_filter_row` relies on.
    Check-in, previous and done tasks belong to the developer's day rather than
    to any one task, so they ride on the first part only instead of repeating.
    """
    def take(field: str) -> list:
        values = row.get(field) or []
        return [values[i] for i in indices] if values else []

    return {
        **row,
        "name": row["name"] if first else f"{row['name']} (continued)",
        "previous": row["previous"] if first else [],
        "done": row["done"] if first else [],
        "picked": take("picked"),
        "task_status": take("task_status"),
        "jira": take("jira"),
        "comments": take("comments"),
    }


def split_row(row: dict, jira_configured: bool, limit: int = SLACK_CHUNK_CHARS) -> list[dict]:
    """One developer's row, split on task boundaries until each part fits.

    `chunk` packs whole blocks and never splits one, so a developer carrying
    many tasks produced a single message far past the limit — 40 tasks rendered
    13k characters against a 3.5k target. Splitting here, before rendering,
    keeps every task's lines together and keeps the parts index-aligned.

    A single task whose own lines exceed the limit cannot be split further and
    is emitted whole; Slack accepts it, it simply reads long.
    """
    tasks = len(row.get("picked") or [])
    if tasks <= 1 or len(developer_block(row, jira_configured)) <= limit:
        return [row]

    parts: list[dict] = []
    current: list[int] = []
    for index in range(tasks):
        candidate = current + [index]
        too_big = len(developer_block(
            _row_slice(row, candidate, first=not parts), jira_configured)) > limit
        if current and too_big:
            parts.append(_row_slice(row, current, first=not parts))
            current = [index]
        else:
            current = candidate
    if current:
        parts.append(_row_slice(row, current, first=not parts))
    return parts


def compose_blocks(rows: list[dict], today: dt.date, jira_configured: bool,
                   limit: int = SLACK_CHUNK_CHARS) -> list[str]:
    """Return [header, developer block, ...] — the digest, one entry per person.

    A developer with too many tasks for one message becomes several blocks.
    """
    header = f"*Daily Stand-up Summary — {today.strftime('%d-%m-%Y')}*"
    blocks = [header]
    for row in rows:
        for part in split_row(row, jira_configured, limit):
            blocks.append(developer_block(part, jira_configured))
    return blocks


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
        "task_status": ([row["task_status"][i] for i in keep]
                        if row.get("task_status") else []),
        "jira": [row["jira"][i] for i in keep] if row["jira"] else [],
        "comments": [row["comments"][i] for i in keep] if row["comments"] else [],
    }


def route_blocks(rows: list[dict], cfg: SlackConfig, today: dt.date,
                 jira_configured: bool) -> dict[str, list[str]]:
    """Group developer blocks by target channel.

    A developer named in SUMMARY_DEVELOPER_ROUTES posts their whole update to
    that one channel, whatever their issue keys say and whether or not they
    picked anything — they belong to a team, not to a prefix.

    Everyone else is split by issue-key prefix; the slice for each project posts
    to that project's channel (SUMMARY_CHANNEL_ROUTES), with unrouted prefixes
    and task-less developers falling back to the default channel
    (SLACK_CHANNEL_ID). Returns {channel: [header, block, ...]}.
    """
    header = f"*Daily Stand-up Summary — {today.strftime('%d-%m-%Y')}*"
    per_channel: dict[str, list[str]] = {}

    def add(channel: str, block: str) -> None:
        per_channel.setdefault(channel, []).append(block)

    for row in rows:
        owned = cfg.channel_for_developer(row["name"])
        if owned:
            # Unsplit: their whole update, including any cross-project tasks.
            for part in split_row(row, jira_configured):
                add(owned, developer_block(part, jira_configured))
            continue

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

    # --date backfills a past day; without it, today.
    try:
        today = day_from_argv(sys.argv)
    except ValueError as exc:
        raise SystemExit(str(exc))
    if today != dt.date.today():
        print(f"Backfill: composing the digest for {today.strftime('%d-%m-%Y')}.")

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
