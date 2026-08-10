"""Build a month-wise attendance matrix in Google Sheets from Slack roll-calls.

Source: the check-in Slack channel (SLACK_CHANNEL_ID).
  * Attendance comes from roll-call posts, one line per person, e.g.
        GN-Present(Full Day)
        Soma-Absent
        Raghul-Half Day
  * Leave comes from any message containing the word "leave" + a date
    (DD-MM-YYYY), e.g. "26-06-2026 is on Leave" -> that person is on Leave that day.

Output: a Google Sheet (ATTENDANCE_SPREADSHEET_ID) with:
  * one tab per month (e.g. "June"), a matrix of Name (rows) x calendar day
    (columns, header MM-DD-YY). Each cell is Present / Absent / Half Day /
    Weekend / Leave / blank, colour-coded. Sat & Sun are auto-marked Weekend.
  * a "Summary" tab: per-person Absent-day count for each month (Jan-Dec).
Present cells where the person did not post a status update that day carry a
cell note: "Status not provided by the developer".

Run:  .venv/Scripts/python.exe build_attendance.py
"""

from __future__ import annotations

import calendar
import datetime as dt
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent / "src"))

from standup_summarizer.config import SlackConfig  # noqa: E402
from standup_summarizer.fetch import (  # noqa: E402
    _SlackFetcher,
    _is_standup_message,
    build_client,
)

LOOKBACK_DAYS = 120
NO_STATUS = "Status not provided by the developer"

DEFAULT_SPREADSHEET_ID = "1W3H2uMFG__KTSXDw0trJi71M1RahQS65_bao8EC4sOI"

# Cell background colours (hex).
COLOURS = {
    "Present": "C6EFCE",   # light green
    "Absent": "FFC7CE",    # light red
    "Half Day": "FFEB9C",  # light yellow
    "Weekend": "D9D9D9",   # grey
    "Leave": "FCE4D6",     # light orange
}
HEADER_BG = "1F4E78"

STATUS_MARKERS = (
    "what is task", "what got moved", "what is moved", "what got done",
    "what's next", "whats next",
    "why it matters", "blockers", "project:", "date:", "value:", "task:",
)

_ROLLCALL_LINE = re.compile(r"^\s*([A-Za-z][\w .]*?)\s*[-–—:]\s*(.+?)\s*$")

# A roll-call entry is "Name-Status": a short name and a short status token
# ("Present(Full Day)", "Absent", "Half day") — never prose. Stand-up posts are
# full of "Label: sentence" lines, so without these bounds a line like
# "What value will it add: ... on behalf of ..." parses as an attendance entry
# (be-HALF) and invents a team member.
MAX_NAME_WORDS = 3
MAX_STATUS_CHARS = 30

# Word-boundary matches, so "behalf"/"presentation"/"absentee" do not count.
_ATTENDANCE_WORDS = (
    ("Absent", re.compile(r"\babsent\b")),
    ("Half Day", re.compile(r"\bhalf\b")),
    ("Present", re.compile(r"\bpresent\b")),
)
_DATE = re.compile(r"(\d{1,2})[-./](\d{1,2})[-./](\d{2,4})")
_DATE_ABBR = re.compile(r"(\d{1,2})[-/ ]([A-Za-z]{3})[-/ ](\d{2,4})")
_MONTHS = {m.upper(): i for i, m in enumerate(calendar.month_abbr) if m}


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------
def _attendance_of(text: str) -> str | None:
    """The attendance label in a roll-call status token, else None."""
    text = text.strip()
    if len(text) > MAX_STATUS_CHARS:      # prose, not a status token
        return None
    low = text.lower()
    for label, pattern in _ATTENDANCE_WORDS:
        if pattern.search(low):
            return label
    return None


def _rollcall_entry(line: str) -> tuple[str, str] | None:
    """Parse "Soma-Present(Full Day)" into ("Soma", "Present"); None if not one."""
    m = _ROLLCALL_LINE.match(line)
    if not m:
        return None
    name = m.group(1).strip()
    if len(name.split()) > MAX_NAME_WORDS:   # a stand-up field label, not a name
        return None
    att = _attendance_of(m.group(2))
    return (name, att) if att else None


def _parse_date(text: str) -> dt.date | None:
    """Parse the first date found, day-first: DD-MM-YYYY or DD-MMM-YYYY."""
    m = _DATE_ABBR.search(text)
    if m and m.group(2).upper() in _MONTHS:
        d, mo, y = int(m.group(1)), _MONTHS[m.group(2).upper()], int(m.group(3))
    else:
        m = _DATE.search(text)
        if not m:
            return None
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    try:
        return dt.date(y, mo, d)
    except ValueError:
        return None


def _short_of(name: str, mapping: dict[str, str]) -> str:
    """Map a full/author name or roll-call token to a canonical short label."""
    key = name.lower().replace(" ", "")
    for token, short in mapping.items():
        if key.startswith(token) or token in key:
            return short
    return name.strip()


# --------------------------------------------------------------------------
# Fetch + interpret the channel
# --------------------------------------------------------------------------
def fetch_channel(cfg: SlackConfig):
    """Return roster + parsed attendance/status/leave keyed by canonical short name."""
    client = build_client(cfg.bot_token)
    fetcher = _SlackFetcher(client)

    # Human channel members, for name resolution.
    members: list[str] = []
    cursor: str | None = None
    while True:
        resp = client.conversations_members(channel=cfg.channel_id, limit=200, cursor=cursor)
        members += resp.get("members", [])
        cursor = (resp.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor:
            break
    full_names: list[str] = []
    for uid in members:
        try:
            info = client.users_info(user=uid)["user"]
        except Exception:  # noqa: BLE001
            continue
        if info.get("is_bot") or info.get("deleted") or uid == "USLACKBOT":
            continue
        full_names.append(fetcher._resolve_name(uid))

    raw = _fetch_raw(fetcher, cfg.channel_id)

    # roll_calls[date][short] = status ; status_dates[date] = {short} ; leaves[date] = {short}
    roll_calls: dict[dt.date, dict[str, str]] = defaultdict(dict)
    status_dates: dict[dt.date, set[str]] = defaultdict(set)
    leaves: dict[dt.date, set[str]] = defaultdict(set)
    order: list[str] = []  # row order = first appearance in roll-calls

    # Short-name resolution table: token (lowercased, no spaces) -> short label.
    # Roll-call short tokens win; full names map onto whichever short they match.
    name_map: dict[str, str] = {}

    parsed: list[tuple] = []  # (post_date, author_full, text, entries)
    for msg in raw:
        if not _is_standup_message(msg):
            continue
        post_date = dt.datetime.fromtimestamp(float(msg["ts"])).date()
        text = msg.get("text", "") or ""
        author = fetcher._resolve_name(msg["user"])
        entries = []
        for line in text.splitlines():
            if entry := _rollcall_entry(line):
                entries.append(entry)
        parsed.append((post_date, author, text, entries))
        for short, _ in entries:
            name_map.setdefault(short.lower().replace(" ", ""), short)

    # Make sure full member names map to a short label (prefer roll-call short).
    def canon(name: str) -> str:
        return _short_of(name, name_map)

    for post_date, author, text, entries in parsed:
        if entries:  # roll-call message
            for short, att in entries:
                roll_calls[post_date][short] = att
                if short not in order:
                    order.append(short)
            continue
        low = text.lower()
        if re.search(r"\bleave\b", low) and _parse_date(text):
            who = author
            for full in full_names:  # is a specific person named?
                if full.lower() in low or full.lower().replace(" ", "") in low.replace(" ", ""):
                    who = full
                    break
            for d in {_parse_date(line) for line in [text] + text.splitlines() if _parse_date(line)}:
                if d:
                    leaves[d].add(canon(who))
        elif any(mk in low for mk in STATUS_MARKERS):
            status_dates[post_date].add(canon(author))

    return order, roll_calls, status_dates, leaves


def _fetch_raw(fetcher: _SlackFetcher, channel: str) -> list[dict]:
    latest = dt.datetime.now().astimezone()
    oldest = latest - dt.timedelta(days=LOOKBACK_DAYS)
    return fetcher.channel_history(channel, f"{oldest.timestamp():.6f}", f"{latest.timestamp():.6f}")


# --------------------------------------------------------------------------
# Build month matrices
# --------------------------------------------------------------------------
def cell_value(short: str, day: dt.date, roll_calls, leaves) -> str:
    if day.weekday() >= 5:                       # Sat=5, Sun=6
        return "Weekend"
    if short in leaves.get(day, set()):
        return "Leave"
    return roll_calls.get(day, {}).get(short, "")


def build_month(order, roll_calls, status_dates, leaves, year, month):
    """Return (header, rows, notes) for one month.

    notes: {(row_index_1based_excl_header, col_index)} -> note text, for cells
    that are Present/Half Day but the person posted no status that day.
    """
    ndays = calendar.monthrange(year, month)[1]
    days = [dt.date(year, month, d) for d in range(1, ndays + 1)]
    header = ["Name"] + [d.strftime("%m-%d-%y") for d in days]

    rows: list[list[str]] = []
    notes: dict[tuple[int, int], str] = {}
    for r, short in enumerate(order, start=1):       # row 0 is header
        row = [short]
        for c, day in enumerate(days, start=1):      # col 0 is Name
            val = cell_value(short, day, roll_calls, leaves)
            row.append(val)
            if val in ("Present", "Half Day") and short not in status_dates.get(day, set()):
                notes[(r, c)] = NO_STATUS
        rows.append(row)
    return header, rows, notes


def build_summary(order, roll_calls, leaves, year):
    """Total absence per person per month (Absent + Leave): Name | Jan | ... | Dec."""
    header = ["Name"] + [calendar.month_abbr[m] for m in range(1, 13)] + ["Total"]
    rows = []
    for short in order:
        counts = []
        for month in range(1, 13):
            ndays = calendar.monthrange(year, month)[1]
            total_absent = sum(
                1 for d in range(1, ndays + 1)
                if cell_value(short, dt.date(year, month, d), roll_calls, leaves)
                in ("Absent", "Leave")
            )
            counts.append(total_absent)
        rows.append([short] + counts + [sum(counts)])  # year total (Absent + Leave)
    return header, rows


# --------------------------------------------------------------------------
# Google Sheets push
# --------------------------------------------------------------------------
def _rgb(hex_color: str) -> dict:
    return {"red": int(hex_color[0:2], 16) / 255,
            "green": int(hex_color[2:4], 16) / 255,
            "blue": int(hex_color[4:6], 16) / 255}


def push(spreadsheet_id: str, key_path: str, month_tabs: list[tuple], summary):
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(
        key_path, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    sh = gspread.authorize(creds).open_by_key(spreadsheet_id)

    keep = {"Master", "Summary", "Leaves"} | {title for title, *_ in month_tabs}

    # Master first (employee details — seeded once, then preserved), then Summary,
    # then each month.
    ensure_master(sh)
    _write_tab(sh, "Summary", summary[0], summary[1], notes={}, colour_cells=False, index=1,
               title_label="Total Absence = Absent + Leave", emphasize_last_col=True)
    for i, (title, header, rows, notes) in enumerate(month_tabs, start=2):
        _write_tab(sh, title, header, rows, notes=notes, colour_cells=True, index=i)

    # Remove stale tabs (old long-format "Attendance", default "Sheet1", etc.).
    for ws in sh.worksheets():
        if ws.title not in keep:
            sh.del_worksheet(ws)
    return sh.url


# Employee master (seeded once into the "Master" tab; edit it in the sheet after).
# Date of Joining is documentation for whoever reads the sheet — nothing parses
# it, and it sits after Gross Salary so read_master()'s positional columns hold.
MASTER_HEADERS = ["Name", "Employee ID", "Designation", "Gross Salary", "Date of Joining"]
MASTER_SEED = [
    ["GN", 1234, "Engineer", 50000, ""],
    ["Soma", 9999, "Engineer", 50000, ""],
    ["Raghul", 6666, "Engineer", 50000, ""],
    ["Sahil", 3333, "Engineer", 50000, ""],
    ["Gokul", "", "", "", "03-Aug-2026"],
    ["Mallesh", "", "", "", "03-Aug-2026"],
    ["Madhan", "", "", "", "03-Aug-2026"],
]


def ensure_master(sh) -> None:
    """Create the 'Master' tab (before Summary) with seed rows, only if absent.

    Once it exists it is never overwritten — it holds manually-maintained
    employee details (Employee ID, Designation, Gross Salary).
    """
    import gspread

    try:
        sh.worksheet("Master")
        return  # already exists — preserve manual edits
    except gspread.WorksheetNotFound:
        pass

    ws = sh.add_worksheet(title="Master", rows=30, cols=6, index=0)
    ws.update(values=[MASTER_HEADERS] + MASTER_SEED, range_name="A1", value_input_option="RAW")
    ws.freeze(rows=1)
    sid = ws.id
    sh.batch_update({"requests": [
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {
                "backgroundColor": _rgb(HEADER_BG),
                "horizontalAlignment": "CENTER",
                "textFormat": {"bold": True, "foregroundColor": _rgb("FFFFFF")}}},
            "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,textFormat)"}},
        {"repeatCell": {  # Gross Salary as #,##0
            "range": {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": 3, "endColumnIndex": 4},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}}},
            "fields": "userEnteredFormat.numberFormat"}},
        {"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 5},
            "properties": {"pixelSize": 130}, "fields": "pixelSize"}},
    ]})


def _write_tab(sh, title, header, rows, notes, colour_cells, index, title_label=None,
               emphasize_last_col=False):
    import gspread

    offset = 1 if title_label else 0  # extra leading row for the merged title label
    body = ([[title_label]] if title_label else []) + [header] + rows
    nrows, ncols = len(body), len(header)
    try:
        ws = sh.worksheet(title)
        ws.clear()
        ws.resize(rows=max(nrows + 2, 5), cols=max(ncols + 1, 5))
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=nrows + 2, cols=ncols + 1, index=index)

    ws.update(values=body, range_name="A1", value_input_option="RAW")
    # A full-width merged title can't coexist with a frozen column, so skip the
    # frozen Name column when a title label is present.
    ws.freeze(rows=offset + 1, cols=0 if title_label else 1)

    requests: list[dict] = []
    sid = ws.id

    # Optional merged title label row across all columns.
    if title_label:
        requests.append({"mergeCells": {
            "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": ncols},
            "mergeType": "MERGE_ALL"}})
        requests.append({"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {
                "backgroundColor": _rgb("D9E1F2"),
                "horizontalAlignment": "CENTER",
                "textFormat": {"bold": True}}},
            "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,textFormat)"}})

    # Header row styling (bold, dark bg, white centered text).
    requests.append({"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": offset, "endRowIndex": offset + 1},
        "cell": {"userEnteredFormat": {
            "backgroundColor": _rgb(HEADER_BG),
            "horizontalAlignment": "CENTER",
            "textFormat": {"bold": True, "foregroundColor": _rgb("FFFFFF")}}},
        "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,textFormat)"}})
    # Name column bold (data rows).
    requests.append({"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": offset + 1, "startColumnIndex": 0, "endColumnIndex": 1},
        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
        "fields": "userEnteredFormat.textFormat"}})
    # Emphasize the last column (e.g. Summary "Total"): bold + shaded + centered.
    if emphasize_last_col:
        requests.append({"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": offset + 1,
                      "startColumnIndex": ncols - 1, "endColumnIndex": ncols},
            "cell": {"userEnteredFormat": {
                "backgroundColor": _rgb("DDEBF7"),
                "horizontalAlignment": "CENTER",
                "textFormat": {"bold": True}}},
            "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,textFormat)"}})

    if colour_cells:
        # Centre the data grid.
        requests.append({"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": offset + 1, "startColumnIndex": 1},
            "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}},
            "fields": "userEnteredFormat.horizontalAlignment"}})
        # Clear existing conditional-format rules, then colour by value.
        meta = next((s for s in sh.fetch_sheet_metadata().get("sheets", [])
                     if s["properties"]["sheetId"] == sid), {})
        for _ in meta.get("conditionalFormats", []):
            requests.append({"deleteConditionalFormatRule": {"sheetId": sid, "index": 0}})
        grid = {"sheetId": sid, "startRowIndex": offset + 1, "endRowIndex": nrows,
                "startColumnIndex": 1, "endColumnIndex": ncols}
        for text, hexc in COLOURS.items():
            requests.append({"addConditionalFormatRule": {"index": 0, "rule": {
                "ranges": [grid],
                "booleanRule": {
                    "condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": text}]},
                    "format": {"backgroundColor": _rgb(hexc)}}}}})
        # Column widths: Name wider, day columns narrow.
        requests.append({"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 110}, "fields": "pixelSize"}})
        requests.append({"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 1, "endIndex": ncols},
            "properties": {"pixelSize": 78}, "fields": "pixelSize"}})

    # Cell notes for "status not provided" (shifted by the title label, if any).
    for (r, c), note in notes.items():
        requests.append({"updateCells": {
            "range": {"sheetId": sid, "startRowIndex": r + offset, "endRowIndex": r + offset + 1,
                      "startColumnIndex": c, "endColumnIndex": c + 1},
            "rows": [{"values": [{"note": note}]}], "fields": "note"}})

    if requests:
        sh.batch_update({"requests": requests})


# --------------------------------------------------------------------------
def main() -> None:
    load_dotenv(Path(__file__).parent / ".env")
    cfg = SlackConfig.from_env()

    spreadsheet_id = os.environ.get("ATTENDANCE_SPREADSHEET_ID", DEFAULT_SPREADSHEET_ID)
    key_path = os.environ.get("GOOGLE_SA_KEY_PATH", "credentials/google_credentials.json")
    if not Path(key_path).exists():
        sys.exit(f"Service-account key not found at {key_path}")

    order, roll_calls, status_dates, leaves = fetch_channel(cfg)
    if not order:
        sys.exit("No roll-call posts found — nothing to build.")

    # Months with data (from roll-calls or leaves), plus the upcoming month so
    # its empty tab is always pre-created.
    months = {(d.year, d.month) for d in list(roll_calls) + list(leaves)}
    today = dt.date.today()
    upcoming = (today.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
    months.add((upcoming.year, upcoming.month))
    months = sorted(months)

    month_tabs = []
    for year, month in months:
        header, rows, notes = build_month(order, roll_calls, status_dates, leaves, year, month)
        title = dt.date(year, month, 1).strftime("%B")  # e.g. "June"
        month_tabs.append((title, header, rows, notes))

    data_years = {d.year for d in list(roll_calls) + list(leaves)}
    year = max(data_years) if data_years else today.year
    summary = build_summary(order, roll_calls, leaves, year)

    url = push(spreadsheet_id, key_path, month_tabs, summary)
    flagged = sum(len(n) for _, _, _, n in month_tabs)
    _log(
        f"Pushed {len(order)} member(s) across {len(month_tabs)} month tab(s) "
        f"({', '.join(t for t, *_ in month_tabs)}) + Summary; "
        f"{flagged} '{NO_STATUS}' note(s)."
    )
    _log(f"URL: {url}")

    # Post the daily confirmation to the channel only after the sheet is updated.
    # Opt-in (so test runs don't spam); enable with --notify or NOTIFY_SLACK=1.
    if "--notify" in sys.argv or os.environ.get("NOTIFY_SLACK"):
        _log(notify_slack(cfg, today in roll_calls))


LOG_FILE = Path(__file__).parent / "logs" / "attendance.log"


def _log(message: str) -> None:
    """Print to stdout and append a timestamped line to logs/attendance.log."""
    print(message)
    try:
        LOG_FILE.parent.mkdir(exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {message}\n")
    except Exception:  # noqa: BLE001 — never let logging break the run
        pass


def notify_slack(cfg: SlackConfig, recorded_today: bool) -> str:
    """Post the daily attendance-recorded confirmation; return a status message."""
    if not recorded_today:
        return "No roll-call for today — skipping Slack notification."
    client = build_client(cfg.bot_token)
    message = "Your attendance for today has been recorded successfully."
    try:
        resp = client.chat_postMessage(channel=cfg.channel_id, text=message)
        return f"Posted Slack notification (ts={resp.get('ts')})."
    except Exception as exc:  # noqa: BLE001
        hint = ""
        if "missing_scope" in str(exc):
            hint = "  -> Add the 'chat:write' bot scope to the Slack app and reinstall it."
        return f"Slack notification failed: {exc}{hint}"


if __name__ == "__main__":
    main()
