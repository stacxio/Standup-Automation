"""Build the standup report by summarizing live Slack check-ins, then push to Sheets.

Pipeline: fetch the check-in channel -> group each person's posts by date ->
summarize them into structured columns with the configured reasoning engine
(local Ollama / OpenAI-compatible / Anthropic) -> derive a Status -> write a CSV
and idempotently rewrite the Google Sheet tab.

Config (.env):
  SLACK_BOT_TOKEN, SLACK_CHANNEL_ID                     # the check-in channel
  REASONING_BACKEND (local|openai|anthropic), REASONING_MODEL,
    REASONING_BASE_URL, REASONING_API_KEY, REASONING_MAX_RETRIES
  GOOGLE_SA_KEY_PATH, SPREADSHEET_ID, SHEET_TAB_NAME    # the push target
  REPORT_LOOKBACK_DAYS (default 30)                     # how far back to summarize

Run:  .venv/Scripts/python.exe build_report.py
"""

from __future__ import annotations

import csv
import datetime as dt
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

from standup_summarizer import jira  # noqa: E402
from standup_summarizer.config import JiraConfig, ReasoningConfig, SlackConfig  # noqa: E402
from standup_summarizer.engines import build_engine  # noqa: E402
from standup_summarizer.fetch import (  # noqa: E402
    _SlackFetcher,
    _is_standup_message,
    build_client,
)
from standup_summarizer.summarize import summarize  # noqa: E402

HEADERS = [
    "Developer Name", "Date", "Projectname", "Task", "Value",
    "What got moved?", "Why it matters?", "Blockers?", "What's Next?", "Status",
    "Picked Tasks", "Completed Tasks",
]

# Jira issue keys, e.g. "PROJ-123". Matched case-insensitively then upper-cased.
# Stand-ups are hand-typed, so spaces around the hyphen are common ("HIR - 72",
# "BHA- 79", "WS - 186") — those are accepted too, but only for keys that look
# like real project keys (see extract_jira_ids).
_JIRA_ID = re.compile(r"\b([A-Za-z][A-Za-z0-9]+)[ \t]*-[ \t]*(\d+)\b")
# Max length of an all-caps project key accepted with spaces around the hyphen.
_MAX_SPACED_KEY = 10

LOOKBACK_DAYS = int(os.environ.get("REPORT_LOOKBACK_DAYS", "30") or 30)

# Markers that identify a real stand-up post (vs. roll-call / leave / chatter).
_STANDUP_MARKERS = (
    "what is task", "what got moved", "what is moved", "what's next", "whats next",
    "why it matters", "blockers", "project:", "date:", "value:", "task:", "moved:",
)
# Attendance roll-call lines, e.g. "GN-Present(Full Day)" — excluded from the report.
# Bounded name + status and word-boundary keywords, matching build_attendance's
# rule: an unbounded ".*(present|half)" also matches stand-up prose ("...on
# behalf of...", "...the presentation..."), which would drop a real stand-up.
_ROLLCALL_LINE = re.compile(
    r"^\s*[A-Za-z][\w .]{0,20}?\s*[-–—:]\s*[^\n]{0,30}?\b(present|absent|half\s*day)\b",
    re.I,
)


# --------------------------------------------------------------------------
# Fetch + group live stand-up posts
# --------------------------------------------------------------------------
def fetch_standup_entries(cfg: SlackConfig):
    """Return (entries, texts).

    entries: [{developer, date, text}] — one per person per day, for the engine.
    texts:   {(developer, date): combined text} — used to derive Status.
    """
    fetcher = _SlackFetcher(build_client(cfg.bot_token))
    latest = dt.datetime.now().astimezone()
    oldest = latest - dt.timedelta(days=LOOKBACK_DAYS)
    raw = fetcher.channel_history(
        cfg.channel_id, f"{oldest.timestamp():.6f}", f"{latest.timestamp():.6f}"
    )

    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for msg in raw:
        if not _is_standup_message(msg):  # drop bots/system/joins/empty
            continue
        text = msg.get("text", "") or ""
        if any(_ROLLCALL_LINE.match(line) for line in text.splitlines()):
            continue  # attendance roll-call, not a stand-up
        date = dt.datetime.fromtimestamp(float(msg["ts"])).strftime("%Y-%m-%d")
        groups[(fetcher._resolve_name(msg["user"]), date)].append(text)

    entries, texts = [], {}
    for (name, date), parts in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        combined = "\n".join(parts).strip()
        if not any(m in combined.lower() for m in _STANDUP_MARKERS):
            continue  # skip pure chatter — only summarize real stand-ups
        entries.append({"developer": name, "date": date, "text": combined})
        texts[(name, date)] = combined
    return entries, texts


# --------------------------------------------------------------------------
# Status derivation (deterministic, from the raw text)
# --------------------------------------------------------------------------
def _blockers_reported(text: str) -> bool:
    m = re.search(
        r"blocker[s]?\b\s*[:?]?\s*[-—•]?\s*(.*?)(?:what'?\s*s?\s*next|what next|$)",
        text, re.I | re.S,
    )
    if not m:
        return False
    seg = re.sub(r"[*_`>]", "", m.group(1)).strip().strip("-—•:?. ").strip().lower()
    if seg in ("", "no", "none", "nil", "na", "n/a", "nothing"):
        return False
    if seg.startswith("none blocking") or seg.startswith("no blocker"):
        return False
    return True


def _derive_status(text: str) -> str:
    low = text.lower()
    if _blockers_reported(low):
        return "Blocked"
    if any(s in low for s in ("got moved", "is moved", "moved:", "moved —", "moved -")):
        return "Completed"
    return "In Progress"


def extract_jira_ids(text: str) -> list[str]:
    """Return the unique Jira keys mentioned in `text`, normalised, in order.

    "HIR - 72" and "BHA- 79" normalise to HIR-72 / BHA-79. Spacing is only
    tolerated for short all-caps keys, so ordinary prose ("Best for page - 2")
    is not mistaken for an issue id.
    """
    seen: dict[str, None] = {}
    for m in _JIRA_ID.finditer(text or ""):
        key, number = m.group(1), m.group(2)
        spaced = " " in m.group(0) or "\t" in m.group(0)
        if spaced and not (key.isupper() and len(key) <= _MAX_SPACED_KEY):
            continue
        seen.setdefault(f"{key.upper()}-{number}", None)
    return list(seen)


# Shown in Status when a person checked in but referenced no Jira issue.
NO_JIRA_ID = "Developer not update the jira Task id."
# Shown for a key Jira couldn't resolve (not found / no access).
UNKNOWN_STATUS = "Unknown"


def _jira_status(key: str, jira_cfg, cache: dict[str, tuple | None]) -> tuple | None:
    """(status_name, category) for a Jira key, cached so each key is fetched once."""
    if key not in cache:
        cache[key] = jira.issue_status(jira_cfg, key)
    return cache[key]


def build_rows(records: list[dict], texts: dict, jira_cfg=None) -> list[list[str]]:
    """Turn engine records into deduplicated rows.

    Per (developer, date) the Jira keys mentioned in the day's posts are the
    Picked Tasks. Each key is looked up live in Jira:
      * Status         -> consolidated "KEY <live status>" for every picked key
                          (e.g. "WS-174 In Review, WS-175 Done"), or a notice
                          when no key was mentioned.
      * Completed Tasks -> the subset whose status category is 'done'.
    Without Jira configured, Status falls back to the text-derived value and
    Completed Tasks is blank.
    """
    seen: set[tuple] = set()
    rows: list[list[str]] = []
    status_cache: dict[str, tuple | None] = {}
    for r in records:
        combined = texts.get((r["developer"], r["date"]), "")
        picked = extract_jira_ids(combined)

        if not jira_cfg:
            status, completed = _derive_status(combined), []
        elif not picked:
            status, completed = NO_JIRA_ID, []
        else:
            parts, completed = [], []
            for key in picked:
                st = _jira_status(key, jira_cfg, status_cache)
                parts.append(f"{key} {st[0] if st else UNKNOWN_STATUS}")
                if st and st[1] == "done":
                    completed.append(key)
            status = ", ".join(parts)

        row = [r["developer"], r["date"], r["project"], r["task"], r["value"],
               r["what_got_moved"], r["why_it_matters"], r["blockers"], r["whats_next"],
               status, ", ".join(picked), ", ".join(completed)]
        key = tuple(row)
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    return rows


# --------------------------------------------------------------------------
# CSV + Google Sheets push
# --------------------------------------------------------------------------
def write_csv(values: list[list[str]], path: Path) -> None:
    path.parent.mkdir(exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        csv.writer(fh).writerows(values)


def _rgb(hex_color: str) -> dict:
    r, g, b = (int(hex_color[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return {"red": r, "green": g, "blue": b}


def push_to_sheets(values: list[list[str]]) -> str | None:
    """Write one tab per month (e.g. 'July2026'); return the spreadsheet URL.

    Rows are split by their Date into month tabs. Each month tab is cleared and
    rewritten (idempotent). Past month tabs outside the fetch window are left
    untouched (history accumulates); the legacy single 'Sheet1' is removed.
    """
    key_path = Path(os.environ.get("GOOGLE_SA_KEY_PATH", "credentials/google_credentials.json"))
    if not key_path.is_absolute():
        key_path = ROOT / key_path
    spreadsheet_id = os.environ.get("SPREADSHEET_ID", "").strip()

    if not spreadsheet_id or not key_path.exists():
        print(
            "Skipping Google Sheets push — not configured.\n"
            f"  - service-account key: {'found' if key_path.exists() else 'MISSING at ' + str(key_path)}\n"
            f"  - SPREADSHEET_ID: {'set' if spreadsheet_id else 'MISSING'}"
        )
        return None

    import gspread
    from google.oauth2.service_account import Credentials

    header, rows = values[0], values[1:]
    by_month = group_by_month(rows)

    creds = Credentials.from_service_account_file(
        str(key_path), scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    sh = gspread.authorize(creds).open_by_key(spreadsheet_id)

    written = []
    for idx, (year, month) in enumerate(sorted(by_month)):
        title = dt.date(year, month, 1).strftime("%B%Y")  # e.g. "July2026"
        mvalues = [header] + by_month[(year, month)]
        try:
            ws = sh.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=title, rows=len(mvalues) + 10, cols=len(HEADERS), index=idx)
        ws.clear()
        ws.resize(rows=max(len(mvalues) + 5, 10), cols=max(len(HEADERS) + 1, 12))
        ws.update(values=mvalues, range_name="A1", value_input_option="RAW")
        _apply_formatting(sh, ws)
        written.append((title, len(mvalues) - 1))

    # Remove the legacy single tab (e.g. "Sheet1"); keep every month-named tab.
    # Only the default gspread/Sheets names are removed: this spreadsheet is
    # shared with the other agents, whose tabs (Gap Report, Standup
    # Verification, Meeting Archive, Scorecard *) are not month-named and must
    # survive a report run. Deleting anything non-month-named would wipe them.
    for ws in sh.worksheets():
        if not _is_month_tab(ws.title) and _LEGACY_TAB.match(ws.title):
            sh.del_worksheet(ws)

    for title, n in written:
        print(f"  {title}: {n} row(s)")
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"


def group_by_month(rows: list[list[str]]) -> dict[tuple[int, int], list]:
    """Group data rows by (year, month) from their Date column (col index 1)."""
    by_month: dict[tuple[int, int], list] = defaultdict(list)
    for row in rows:
        try:
            d = dt.date.fromisoformat(row[1])
        except (ValueError, IndexError):
            continue  # skip rows without a parseable date
        by_month[(d.year, d.month)].append(row)
    return by_month


# Default sheet names created by Google Sheets / gspread — safe to delete.
_LEGACY_TAB = re.compile(r"^(Sheet\d*|Sheet1|Copy of Sheet\d*)$", re.I)


def _is_month_tab(name: str) -> bool:
    """True if `name` looks like a '%B%Y' month tab (e.g. 'July2026')."""
    try:
        dt.datetime.strptime(name, "%B%Y")
        return True
    except ValueError:
        return False


def _apply_formatting(sh, ws) -> None:
    try:
        last_col = chr(ord("A") + len(HEADERS) - 1)
        ws.freeze(rows=1)
        ws.format(f"A1:{last_col}1", {
            "textFormat": {"bold": True, "foregroundColor": _rgb("FFFFFF")},
            "backgroundColor": _rgb("1F4E78"),
            "horizontalAlignment": "CENTER",
        })
        # Status is now free-form per-issue Jira text, so it's no longer colour
        # coded. Clear any legacy conditional-format rules from earlier runs.
        existing = next(
            (s for s in sh.fetch_sheet_metadata().get("sheets", []) if s["properties"]["sheetId"] == ws.id),
            {},
        ).get("conditionalFormats", [])
        if existing:
            sh.batch_update({"requests": [
                {"deleteConditionalFormatRule": {"sheetId": ws.id, "index": 0}} for _ in existing]})
    except Exception as exc:  # noqa: BLE001 — formatting must never fail the data push
        print(f"  (formatting skipped: {exc})")


def main() -> None:
    load_dotenv(ROOT / ".env")
    slack_cfg = SlackConfig.from_env()
    reasoning_cfg = ReasoningConfig.from_env()

    jira_cfg = JiraConfig.from_env()
    if jira_cfg:
        print(f"Jira status checks enabled ({jira_cfg.base_url}).")
    else:
        print("Jira not configured — Completed Tasks will be blank "
              "(set JIRA_BASE_URL/JIRA_EMAIL/JIRA_API_TOKEN in .env to enable).")

    entries, texts = fetch_standup_entries(slack_cfg)
    print(f"Fetched {len(entries)} stand-up entr(ies) from the last {LOOKBACK_DAYS} days.")
    if not entries:
        print("Nothing to summarize.")
        return

    print(f"Summarizing via backend={reasoning_cfg.backend} model={reasoning_cfg.model} ...")
    try:
        engine = build_engine(reasoning_cfg)
        records = summarize(entries, engine, reasoning_cfg.max_retries)
    except Exception as exc:  # noqa: BLE001 — surface a clear, actionable message
        print(f"\nReasoning engine unavailable — {type(exc).__name__}: {exc}")
        if reasoning_cfg.backend in {"local", "ollama", "vllm"}:
            print(
                f"  The 'local' backend needs an OpenAI-compatible server at {reasoning_cfg.base_url}.\n"
                f"  Start Ollama and `ollama pull {reasoning_cfg.model}`, or set a hosted backend\n"
                "  (REASONING_BACKEND=anthropic|openai + REASONING_API_KEY) in .env."
            )
        else:
            print("  Check REASONING_API_KEY / REASONING_MODEL / REASONING_BASE_URL in .env.")
        sys.exit(2)  # exit 2 = "skipped" (backend not configured) — not a hard failure

    rows = build_rows(records, texts, jira_cfg)
    values = [HEADERS] + rows

    csv_path = ROOT / "Result" / "standup_report.csv"
    write_csv(values, csv_path)
    print(f"Wrote {len(rows)} rows -> {csv_path}")

    url = push_to_sheets(values)
    if url:
        print(f"Pushed {len(rows)} rows to Google Sheets -> {url}")

    # Notify the channel only when the sheet was actually updated (url set).
    # Opt-in (so test runs don't spam); enable with --notify or NOTIFY_SLACK=1.
    if "--notify" in sys.argv or os.environ.get("NOTIFY_SLACK"):
        print(notify_slack(slack_cfg, url))


def notify_slack(cfg: SlackConfig, url: str | None) -> str:
    """Post 'Stand-up updated successfully!' to the channel after a sheet update."""
    if not url:
        return "Sheet not updated — skipping Slack notification."
    client = build_client(cfg.bot_token)
    try:
        resp = client.chat_postMessage(channel=cfg.channel_id, text="Stand-up updated successfully!")
        return f"Posted Slack notification (ts={resp.get('ts')})."
    except Exception as exc:  # noqa: BLE001
        hint = ""
        if "missing_scope" in str(exc):
            hint = "  -> Add the 'chat:write' bot scope to the Slack app and reinstall it."
        return f"Slack notification failed: {exc}{hint}"


if __name__ == "__main__":
    main()
