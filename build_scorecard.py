"""Daily performance scorecard agent — gather facts, score, publish.

Implements docs/SCORING.md. All the arithmetic lives in
`standup_summarizer.scorecard`, which is pure; this file is the I/O half:
Slack in, Jira in, Sheets and Slack out. Nothing here computes a score.

Three modes, all non-interactive and safe to re-run:

  --capture     Freeze the day's committed tasks. Reads the check-in channel,
                extracts the Jira keys each developer named in their stand-up,
                and writes them to the 'Scorecard Commitments' tab. Runs at
                SCORE_CAPTURE_HOUR (~11:00), before the workday closes, so a
                commitment cannot be quietly dropped later in the day.

  --score       Score the day against that frozen set using Jira state at the
                cutoff. Writes 'Scorecard Daily' (append-only) and the month
                tab, DMs each developer their breakdown and posts the team
                aggregate. Runs at SCORE_CUTOFF_HOUR (~18:00).

  --recompute   Re-run --score over the trailing SCORE_RECOMPUTE_DAYS days.
                This is the self-correcting pass that replaces an appeals
                process: a Jira update made late in the evening is picked up
                overnight without anyone having to ask. It is the same
                deterministic function on fresher data, not a second opinion.

Config: see the SCORE_* block in .env.example.

Run:
  .venv/Scripts/python.exe build_scorecard.py --capture
  .venv/Scripts/python.exe build_scorecard.py --score
  .venv/Scripts/python.exe build_scorecard.py --score --date 2026-08-11 --dry-run
  .venv/Scripts/python.exe build_scorecard.py --recompute --days 3
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

from standup_summarizer import gsheets, jira, scorecard as sc  # noqa: E402
from standup_summarizer.config import (  # noqa: E402
    ArchiveConfig,
    JiraConfig,
    ScoreConfig,
    SlackConfig,
)
from standup_summarizer.fetch import build_client  # noqa: E402

import build_attendance as att  # noqa: E402
import build_report as rep  # noqa: E402

COMMITMENTS_TAB = "Scorecard Commitments"
COMMITMENT_HEADERS = ["Date", "Developer", "Picked Tasks", "Captured At"]
DAILY_TAB = "Scorecard Daily"
MONTH_TAB_FMT = "Scores%B%Y"          # e.g. "ScoresAugust2026"
HEADER_BG = "1F4E78"

# How much history the under-commitment guard needs before it does anything
# (SCORING.md §5.1) — a median from three days would punish a normal week.
MEDIAN_WINDOW_DAYS = 30
MEDIAN_MIN_DAYS = 10


# --------------------------------------------------------------------------
# Slack: roster, attendance, and what each developer said
# --------------------------------------------------------------------------
def read_channel(cfg: SlackConfig):
    """Roster + attendance + stand-up text, one pass over the check-in channel."""
    order, roll_calls, _status_dates, leaves = att.fetch_channel(cfg)
    name_map = {s.lower().replace(" ", ""): s for s in order}
    entries, _texts = rep.fetch_standup_entries(cfg)

    standups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for entry in entries:
        short = att._short_of(entry["developer"], name_map)
        standups[(entry["date"], short)].append(entry["text"])
    return order, roll_calls, leaves, name_map, standups


def picked_from_slack(standups: dict, day: dt.date, developer: str,
                      prefixes: frozenset[str] | None = None) -> list[str]:
    """The Jira keys a developer named in their stand-up — their commitment.

    Reuses the report's tolerant parser, so hand-typed forms ("HIR - 72",
    "BHA- 79") resolve the same way they do everywhere else, then keeps only
    keys whose prefix names a real project (SCORING.md §4.2).

    The filter matters more than it looks: stand-ups are prose, and the parser
    matches anything shaped like `ABC-123`. Real stand-ups produced "ST-11",
    "PI-01", "ORG-404" and "TEMP-2" — none of them Jira issues. Because a key
    Jira cannot resolve sends the whole day to `Not Scored`, one such phrase
    silently costs that developer their score for the day.
    """
    text = "\n".join(standups.get((day.isoformat(), developer), []))
    keys = rep.extract_jira_ids(text)
    if not prefixes:
        return keys
    return [k for k in keys if k.split("-", 1)[0].upper() in prefixes]


def attendance_of(developer: str, day: dt.date, roll_calls, leaves) -> tuple[str, bool]:
    """Return (attendance, checked_in) for one developer-day.

    `att.cell_value` returns "" when the person has no roll-call entry. That is
    deliberately *not* read as absence: it may equally mean no roll-call was
    posted at all, or that one was posted and they were left off it. Either way
    it is a missing fact, and the caller turns it into `Not Scored` rather than
    a zero (SCORING.md §7).
    """
    value = att.cell_value(developer, day, roll_calls, leaves)
    return value, developer in roll_calls.get(day, {})


# --------------------------------------------------------------------------
# Jira: the facts behind each per-task check
# --------------------------------------------------------------------------
class JiraFacts:
    """Fetches a task's facts once per run and caches them by key.

    A key named by three developers costs one round trip, not three. `failed`
    records keys Jira could not answer for, which the caller turns into
    `Not Scored` rather than scoring the developer on partial data.
    """

    def __init__(self, cfg: JiraConfig | None, name_map: dict[str, str]) -> None:
        self.cfg = cfg
        self.name_map = name_map
        self._cache: dict[str, dict | None] = {}
        self._parents: dict[str, str] = {}
        self.failed: set[str] = set()

    def raw(self, key: str) -> dict | None:
        if key in self._cache:
            return self._cache[key]
        self._cache[key] = fetched = self._fetch(key)
        if fetched is None:
            self.failed.add(key)
        return fetched

    def _fetch(self, key: str) -> dict | None:
        if not self.cfg:
            return None
        detail = jira.issue_detail(self.cfg, key)
        if detail is None:
            return None
        comments = jira.issue_comments(self.cfg, key)
        if comments is None:
            return None
        dev = jira.issue_dev_info(self.cfg, detail.get("id", "")) if detail.get("id") else {}
        linked = bool(dev.get("commits") or dev.get("branches") or dev.get("pull_requests"))
        return {"detail": detail, "comments": comments, "has_linked_commit": linked,
                "parent_description": self._parent_description(detail)}

    def _parent_description(self, detail: dict) -> str:
        """The parent's description, for a sub-task that carries none itself.

        Only fetched when the issue's own description is thin, so a well
        documented ticket costs no extra round trip. A failed parent lookup is
        not a data failure — the check simply falls back to what the sub-task
        itself has.
        """
        parent_key = detail.get("parent_key")
        if not parent_key or len(str(detail.get("description") or "").strip()) >= 30:
            return ""
        if parent_key not in self._parents:
            parent = jira.issue_detail(self.cfg, parent_key)
            self._parents[parent_key] = (parent or {}).get("description", "")
        return self._parents[parent_key]

    def task(self, key: str, developer: str) -> sc.TaskFacts | None:
        """Assemble one `TaskFacts`, or None when Jira could not answer."""
        raw = self.raw(key)
        if raw is None:
            return None
        detail = raw["detail"]
        comments = []
        for comment in raw["comments"]:
            created = _comment_date(comment.get("created", ""))
            if created is None:
                continue
            author = att._short_of(comment.get("author", ""), self.name_map)
            comments.append(sc.CommentFacts(
                body=comment.get("body", ""),
                created=created,
                authored_by_developer=(author == developer),
                media=tuple(comment.get("media") or ()),
            ))
        return sc.TaskFacts(
            key=detail.get("key", key),
            description=detail.get("description", ""),
            parent_description=raw.get("parent_description", ""),
            status=detail.get("status_name", ""),
            status_category=_category(detail.get("status_category", "")),
            issue_type=detail.get("issue_type", ""),
            has_linked_commit=raw["has_linked_commit"],
            comments=tuple(comments),
            url=f"{self.cfg.base_url}/browse/{detail.get('key', key)}" if self.cfg else "",
        )


def _comment_date(created: str) -> dt.date | None:
    """Jira 'created' is "2026-07-21T10:15:30.123+0530" — the date is the first 10."""
    try:
        return dt.date.fromisoformat(str(created)[:10])
    except ValueError:
        return None


def _category(key: str) -> str:
    """Jira's category key ('new'|'indeterminate'|'done') -> the scorer's vocabulary."""
    return {"done": "Done", "indeterminate": "In Progress", "new": "To Do"}.get(key, key)


# --------------------------------------------------------------------------
# Assembling a day
# --------------------------------------------------------------------------
def build_day_facts(developer: str, day: dt.date, picked: list[str],
                    roll_calls, leaves, facts: JiraFacts,
                    median: float | None) -> sc.DayFacts:
    attendance, checked_in = attendance_of(developer, day, roll_calls, leaves)

    if not attendance:
        return sc.DayFacts(
            developer=developer, date=day, attendance="Unknown", checked_in=False,
            data_ok=False,
            data_error=("no roll-call posted for the day" if not roll_calls.get(day)
                        else "not listed in the day's roll-call"),
        )

    # Absent or on leave: no Jira lookups are needed or meaningful.
    if sc._norm(attendance) == "absent" or sc._norm(attendance) in sc.UNSCORABLE_ATTENDANCE:
        return sc.DayFacts(developer=developer, date=day, attendance=attendance,
                           checked_in=checked_in)

    if facts.cfg is None:
        return sc.DayFacts(developer=developer, date=day, attendance=attendance,
                           checked_in=checked_in, picked_tasks=tuple(picked),
                           data_ok=not picked,
                           data_error="jira not configured" if picked else "")

    tasks = [t for t in (facts.task(k, developer) for k in picked) if t is not None]
    return sc.DayFacts(
        developer=developer, date=day, attendance=attendance, checked_in=checked_in,
        picked_tasks=tuple(picked), tasks=tuple(tasks), median_picked=median,
    )


def median_picked(history: list[list[str]], developer: str, upto: dt.date) -> float | None:
    """Trailing median committed-task count, from the developer's own history.

    Only scored days inside the window count, and only once there are enough of
    them: a median drawn from three days would punish an ordinary week. Returns
    None when the history is too thin, which leaves the guard dormant.
    """
    picked_col = sc.DAILY_HEADERS.index("Tasks Picked")
    status_col = sc.DAILY_HEADERS.index("Status")
    window_start = upto - dt.timedelta(days=MEDIAN_WINDOW_DAYS)

    counts = []
    for row in history:
        if len(row) <= max(picked_col, status_col):
            continue
        if row[sc.DEVELOPER_COLUMN] != developer or row[status_col] != sc.SCORED:
            continue
        try:
            row_day = dt.date.fromisoformat(row[sc.DATE_COLUMN])
            count = int(float(row[picked_col]))
        except (ValueError, TypeError):
            continue
        if window_start <= row_day < upto:
            counts.append(count)

    if len(counts) < MEDIAN_MIN_DAYS:
        return None
    return float(statistics.median(counts))


# --------------------------------------------------------------------------
# Google Sheets
# --------------------------------------------------------------------------
def open_sheet():
    """The Daily Status spreadsheet, or None when Sheets is not configured."""
    key_path = Path(os.environ.get("GOOGLE_SA_KEY_PATH", "credentials/google_credentials.json"))
    if not key_path.is_absolute():
        key_path = ROOT / key_path
    spreadsheet_id = os.environ.get("SPREADSHEET_ID", "").strip()
    if not spreadsheet_id or not key_path.exists():
        print("Skipping Google Sheets — SPREADSHEET_ID or service-account key not configured.")
        return None, None

    sh = gsheets.open_spreadsheet(spreadsheet_id, key_path)
    return sh, f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"


def read_tab(sh, title: str) -> list[list[str]]:
    """Existing data rows (header dropped); empty when the tab does not exist."""
    if sh is None:
        return []
    import gspread

    try:
        ws = gsheets.retry_api(lambda: sh.worksheet(title), describe=f"open tab '{title}'")
        values = gsheets.retry_api(ws.get_all_values, describe=f"read tab '{title}'")
    except gspread.WorksheetNotFound:
        return []
    return values[1:] if values else []


def _rgb(hex_color: str) -> dict:
    r, g, b = (int(hex_color[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return {"red": r, "green": g, "blue": b}


def write_tab(sh, title: str, header: list[str], rows: list[list[str]]) -> None:
    if sh is None:
        return
    import gspread

    values = [header] + rows
    try:
        ws = gsheets.retry_api(lambda: sh.worksheet(title), describe=f"open tab '{title}'")
    except gspread.WorksheetNotFound:
        ws = gsheets.retry_api(
            lambda: sh.add_worksheet(title=title, rows=len(values) + 20, cols=len(header) + 2),
            describe=f"create tab '{title}'")
    # Clear-then-set, so repeating after a partial write lands on the same result.
    gsheets.retry_api(ws.clear, describe=f"clear '{title}'")
    gsheets.retry_api(
        lambda: ws.resize(rows=max(len(values) + 10, 20), cols=max(len(header) + 1, 8)),
        describe=f"resize '{title}'")
    gsheets.retry_api(
        lambda: ws.update(values=values, range_name="A1", value_input_option="RAW"),
        describe=f"write '{title}'")
    try:
        ws.freeze(rows=1, cols=1)
        ws.format(f"A1:{gspread.utils.rowcol_to_a1(1, len(header))}", {
            "textFormat": {"bold": True, "foregroundColor": _rgb("FFFFFF")},
            "backgroundColor": _rgb(HEADER_BG),
            "horizontalAlignment": "CENTER",
        })
    except Exception as exc:  # noqa: BLE001 — formatting must never fail the data push
        print(f"  (formatting skipped on '{title}': {exc})")


# --------------------------------------------------------------------------
# Slack output
# --------------------------------------------------------------------------
def post_slack(cfg: SlackConfig, channel: str, text: str, label: str) -> None:
    client = build_client(cfg.bot_token)
    try:
        client.chat_postMessage(channel=channel, text=text)
        print(f"  posted {label} -> {channel}")
    except Exception as exc:  # noqa: BLE001
        hint = ""
        if "missing_scope" in str(exc):
            hint = "  -> add the 'chat:write' bot scope and reinstall the app."
        elif "not_in_channel" in str(exc):
            hint = f"  -> invite the bot into {channel}."
        print(f"  Slack post failed ({label}): {exc}{hint}")


def dm_developers(cfg: SlackConfig, records: list[dict],
                  order_ids: dict[str, str]) -> list[str]:
    """DM each developer their own breakdown; return those we could not reach.

    A developer with no Slack id is one who never sees their score. That has to
    surface rather than scroll past in a log — the ids come from the check-in
    channel's membership, so anyone not in that channel is invisible here.
    """
    unreachable: list[str] = []
    for record in records:
        user_id = order_ids.get(record["developer"])
        if not user_id:
            unreachable.append(record["developer"])
            print(f"  no Slack id for {record['developer']} — DM skipped")
            continue
        post_slack(cfg, user_id, sc.compose_slack_dm(record), f"DM {record['developer']}")
    return unreachable


def slack_user_ids(cfg: SlackConfig, name_map: dict[str, str]) -> dict[str, str]:
    """{short_name: Slack user id} for DMs, resolved from channel membership."""
    client = build_client(cfg.bot_token)
    ids: dict[str, str] = {}
    cursor = None
    while True:
        resp = client.conversations_members(channel=cfg.channel_id, limit=200, cursor=cursor)
        for uid in resp.get("members", []):
            try:
                info = client.users_info(user=uid)["user"]
            except Exception:  # noqa: BLE001
                continue
            if info.get("is_bot") or info.get("deleted") or uid == "USLACKBOT":
                continue
            name = info.get("profile", {}).get("real_name") or info.get("real_name") or ""
            ids.setdefault(att._short_of(name, name_map), uid)
        cursor = (resp.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor:
            break
    return ids


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------
def commitments_for(rows: list[list[str]], day: dt.date) -> dict[str, list[str]]:
    """{developer: [keys]} already frozen for `day`."""
    frozen: dict[str, list[str]] = {}
    for row in rows:
        if len(row) < len(COMMITMENT_HEADERS) or row[0] != day.isoformat():
            continue
        frozen[row[1]] = [k.strip() for k in row[2].split(",") if k.strip()]
    return frozen


def merge_commitments(existing: list[list[str]], new_rows: list[list[str]]) -> list[list[str]]:
    """Upsert by (date, developer), newest first — same shape as merge_daily."""
    rows = [list(r) for r in existing if len(r) >= 2 and r[0] and r[1]]
    index = {(r[0], r[1]): i for i, r in enumerate(rows)}
    for row in new_rows:
        key = (row[0], row[1])
        if key in index:
            rows[index[key]] = row
        else:
            index[key] = len(rows)
            rows.append(row)
    rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
    return rows


def project_prefixes(cfg: SlackConfig, score_cfg: ScoreConfig) -> frozenset[str]:
    """Prefixes that name a real Jira project, from the existing routing config."""
    return score_cfg.known_prefixes(cfg.channel_routes, ArchiveConfig.from_env().project_names)


def nudge_candidates(order: list[str], captured: list[tuple[str, list[str]]],
                     day: dt.date, roll_calls, leaves) -> list[str]:
    """Developers to remind: captured just now, with no ticket id, and expected in.

    Nobody is chased on a day they were never expected to work — a weekend, or
    an approved leave — and an explicit Absent is an answer, not a silence. An
    unknown attendance still gets the reminder: at capture time the roll-call is
    often not posted yet, and a missing ticket id is worth flagging either way.
    """
    if day.weekday() >= 5:
        return []
    out = []
    for developer, picked in captured:
        if picked:
            continue
        attendance = att.cell_value(developer, day, roll_calls, leaves)
        if sc._norm(attendance) in sc.UNSCORABLE_ATTENDANCE or sc._norm(attendance) == "absent":
            continue
        out.append(developer)
    return out


def do_capture(day: dt.date, cfg: SlackConfig, score_cfg: ScoreConfig, sh, *,
               dry_run: bool, force: bool) -> dict[str, list[str]]:
    """Freeze each developer's committed tasks for `day`, and chase the gaps."""
    order, roll_calls, leaves, name_map, standups = read_channel(cfg)
    existing = read_tab(sh, COMMITMENTS_TAB)
    frozen = commitments_for(existing, day)
    prefixes = project_prefixes(cfg, score_cfg)

    captured_at = dt.datetime.now().isoformat(timespec="seconds")
    new_rows, captured = [], []
    for developer in order:
        if developer in frozen and not force:
            continue  # already frozen — a re-run must not move the goalposts
        picked = picked_from_slack(standups, day, developer, prefixes)
        frozen[developer] = picked
        captured.append((developer, picked))
        new_rows.append([day.isoformat(), developer, ", ".join(picked), captured_at])

    print(f"Capture {day.isoformat()}: {len(new_rows)} new, "
          f"{len(frozen) - len(new_rows)} already frozen")
    for developer, picked in sorted(frozen.items()):
        print(f"  {developer:<12} {', '.join(picked) if picked else '—'}")

    if new_rows and not dry_run:
        write_tab(sh, COMMITMENTS_TAB, COMMITMENT_HEADERS,
                  merge_commitments(existing, new_rows))

    # Chase only what was captured in *this* run, so a second capture the same
    # day is silent — the same idempotency that stops the snapshot moving.
    chase = nudge_candidates(order, captured, day, roll_calls, leaves)
    if chase and score_cfg.nudge_enabled:
        print(f"No task id yet: {', '.join(chase)}")
        if dry_run:
            print("  (dry run — no reminders sent)")
        else:
            ids = slack_user_ids(cfg, name_map)
            unreachable = []
            for developer in chase:
                user_id = ids.get(developer)
                if not user_id:
                    unreachable.append(developer)
                    continue
                post_slack(cfg, user_id,
                           sc.compose_nudge(developer, day, cfg.channel_id),
                           f"nudge {developer}")
            if unreachable and score_cfg.ops_channel_id:
                post_slack(cfg, score_cfg.ops_channel_id,
                           f"*Task-id reminder — {day.isoformat()}*\nNo Slack id for: "
                           f"{', '.join(unreachable)}. They were not reminded.",
                           "unreachable-developer alert")
    return frozen


def do_score(day: dt.date, cfg: SlackConfig, score_cfg: ScoreConfig, sh, *,
             dry_run: bool, notify: bool) -> list[dict]:
    """Score every roster developer for `day` and publish the result."""
    order, roll_calls, leaves, name_map, standups = read_channel(cfg)
    jira_cfg = JiraConfig.from_env()
    if not jira_cfg:
        print("Jira not configured — days with committed tasks will be Not Scored.")

    # Prefer the frozen commitment; capture one now if the 11:00 run was missed,
    # so there is always an audit trail of what the day was scored against.
    commitments = read_tab(sh, COMMITMENTS_TAB)
    frozen = commitments_for(commitments, day)
    if not frozen:
        print(f"No commitment snapshot for {day.isoformat()} — capturing now.")
        frozen = do_capture(day, cfg, score_cfg, sh, dry_run=dry_run, force=False)

    history = read_tab(sh, DAILY_TAB)
    facts = JiraFacts(jira_cfg, name_map)
    computed_at = dt.datetime.now()

    records = []
    for developer in order:
        picked = frozen.get(developer, [])
        day_facts = build_day_facts(
            developer, day, picked, roll_calls, leaves, facts,
            median_picked(history, developer, day),
        )
        records.append(sc.score_day(day_facts, score_cfg.weights, score_cfg.thresholds,
                                    computed_at=computed_at))

    _print_table(records, day)

    if facts.failed:
        print(f"\n  Jira could not answer for: {', '.join(sorted(facts.failed))}")
        print("  Those developers are Not Scored and will be retried by --recompute.")

    if dry_run:
        # Show the exact message that would be posted. A dry run that hides the
        # output it is dry-running is not much of a check.
        targets = ", ".join(score_cfg.score_channels(cfg.channel_id))
        print(f"\n--- Slack post that would go to {targets} "
              f"({'per-developer' if score_cfg.public_scores else 'aggregate'}) ---")
        print(sc.compose_slack_roster(records, order=score_cfg.public_order)
              if score_cfg.public_scores else sc.compose_slack_team(records))
        print("\n--- Rows that would be written to "
              f"'{DAILY_TAB}' ({len(records)}) ---")
        print("  " + " | ".join(sc.DAILY_HEADERS[:len(sc.DAILY_HEADERS) - 2]))
        for record in records:
            row = sc.daily_row(record)
            print("  " + " | ".join(row[:len(row) - 2]))
        print("\nDry run — not writing to Sheets or posting to Slack.")
        return records

    rows = [sc.daily_row(r) for r in records]
    write_tab(sh, DAILY_TAB, sc.DAILY_HEADERS, sc.merge_daily(history, rows))

    month_records = _records_from_rows(sc.merge_daily(history, rows), day)
    header, matrix = sc.month_matrix(month_records, day.year, day.month, order)
    write_tab(sh, day.strftime(MONTH_TAB_FMT), header, matrix)
    print(f"\nWrote '{DAILY_TAB}' and '{day.strftime(MONTH_TAB_FMT)}'.")

    if notify:
        text = (sc.compose_slack_roster(records, order=score_cfg.public_order)
                if score_cfg.public_scores else sc.compose_slack_team(records))
        label = "daily scorecard" if score_cfg.public_scores else "team aggregate"
        for channel in score_cfg.score_channels(cfg.channel_id):
            post_slack(cfg, channel, text, label)
        if score_cfg.dm_enabled:
            unreachable = dm_developers(cfg, records, slack_user_ids(cfg, name_map))
            if unreachable and score_cfg.ops_channel_id:
                post_slack(cfg, score_cfg.ops_channel_id,
                           f"*Scorecard — {day.isoformat()}*\n"
                           f"No Slack id for: {', '.join(unreachable)}. They did not "
                           f"receive their score. Invite them to <#{cfg.channel_id}> — "
                           f"ids are resolved from that channel's membership.",
                           "unreachable-developer alert")
        elif not score_cfg.public_scores:
            print("  SCORE_DM_ENABLED=false — individual DMs withheld (shadow mode).")
    _report_failures(cfg, records, score_cfg, day, dry_run=dry_run)
    return records


def _records_from_rows(rows: list[list[str]], day: dt.date) -> list[dict]:
    """Minimal records for the month matrix, read back from the fact table."""
    total_col = sc.DAILY_HEADERS.index("Total")
    status_col = sc.DAILY_HEADERS.index("Status")
    attendance_col = sc.DAILY_HEADERS.index("Attendance")
    out = []
    for row in rows:
        if len(row) <= total_col:
            continue
        try:
            row_day = dt.date.fromisoformat(row[sc.DATE_COLUMN])
        except ValueError:
            continue
        if (row_day.year, row_day.month) != (day.year, day.month):
            continue
        try:
            total = float(row[total_col]) if row[total_col] else None
        except ValueError:
            total = None
        out.append({"developer": row[sc.DEVELOPER_COLUMN], "date": row[sc.DATE_COLUMN],
                    "attendance": row[attendance_col], "status": row[status_col],
                    "total": total})
    return out


def _report_failures(cfg: SlackConfig, records: list[dict], score_cfg: ScoreConfig,
                     day: dt.date, *, dry_run: bool) -> None:
    """Alert ops about data failures, so a silent outage cannot become a score."""
    failures = [r for r in records if r["status"] == sc.NOT_SCORED
                and sc._norm(r.get("attendance", "")) not in sc.UNSCORABLE_ATTENDANCE]
    if not failures or dry_run or not score_cfg.ops_channel_id:
        return
    lines = [f"*Scorecard data failures — {day.isoformat()}*", ""]
    lines += [f"  • {r['developer']}: {r['reason']}" for r in failures]
    lines += ["", "_These days are excluded from averages and retried by the nightly pass._"]
    post_slack(cfg, score_cfg.ops_channel_id, "\n".join(lines), "ops alert")


def _print_table(records: list[dict], day: dt.date) -> None:
    print(f"\nScorecard — {day.isoformat()}")
    print(f"  {'Developer':<12} {'Att':<10} {'Proc':>5} {'Del':>6} {'Total':>6}  Band / reason")
    for r in records:
        if r["status"] != sc.SCORED:
            print(f"  {r['developer']:<12} {r['attendance']:<10} {'—':>5} {'—':>6} "
                  f"{'—':>6}  Not Scored: {r['reason']}")
            continue
        print(f"  {r['developer']:<12} {r['attendance']:<10} {r['process']:>5} "
              f"{r['delivery']:>6} {r['total']:>6}  {r['band']}"
              f"{'  [' + ', '.join(r['flags']) + ']' if r['flags'] else ''}")
    average = sc.period_average(records)
    print(f"  {'team average':<12} {'':<10} {'':>5} {'':>6} "
          f"{('—' if average is None else average):>6}")


# --------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Daily performance scorecard.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--capture", action="store_true",
                      help="freeze today's committed tasks (run at SCORE_CAPTURE_HOUR)")
    mode.add_argument("--score", action="store_true",
                      help="score the day and publish (run at SCORE_CUTOFF_HOUR)")
    mode.add_argument("--recompute", action="store_true",
                      help="re-score the trailing window — the self-correcting pass")
    parser.add_argument("--date", help="YYYY-MM-DD (default today)")
    parser.add_argument("--days", type=int, help="window for --recompute (default SCORE_RECOMPUTE_DAYS)")
    parser.add_argument("--force", action="store_true",
                        help="--capture: re-freeze commitments already captured")
    parser.add_argument("--dry-run", action="store_true", help="print only; write nothing")
    parser.add_argument("--notify", action="store_true", help="post to Slack after scoring")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()

    try:
        score_cfg = ScoreConfig.from_env()
    except ValueError as exc:
        raise SystemExit(f"Scoring is misconfigured: {exc}")

    cfg = SlackConfig.from_env()
    sh, url = open_sheet()
    notify = args.notify or bool(os.environ.get("NOTIFY_SLACK"))

    if args.capture:
        do_capture(day, cfg, score_cfg, sh, dry_run=args.dry_run, force=args.force)
    elif args.recompute:
        days = args.days or score_cfg.recompute_days
        for offset in range(days - 1, -1, -1):
            target = day - dt.timedelta(days=offset)
            print(f"\n=== recompute {target.isoformat()} ===")
            do_score(target, cfg, score_cfg, sh, dry_run=args.dry_run, notify=False)
    else:
        do_score(day, cfg, score_cfg, sh, dry_run=args.dry_run, notify=notify)

    if url and not args.dry_run:
        print(f"\n{url}")


if __name__ == "__main__":
    main()
