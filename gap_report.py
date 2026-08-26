"""Stand-up gap audit: compare Slack stand-ups against the meeting transcript.

Access method A (paste / export): you hand the app the meeting transcript as an
Otter `.txt` export (or pasted text). It then:
  * fetches today's Slack stand-ups (same source as the Daily Status report),
  * asks the reasoning engine to compare each developer's Slack update against
    what was actually said in the meeting and list the gaps,
  * writes a 'Gap Report' tab to the Daily Status sheet (SPREADSHEET_ID), and
  * posts a summary to #stacx-check-in (SLACK_CHANNEL_ID).

The transcript comes from you (Otter export / paste) — no Otter login or scrape.

Run:
  .venv/Scripts/python.exe gap_report.py --transcript meeting.txt
  .venv/Scripts/python.exe gap_report.py --transcript meeting.txt --dry-run
  .venv/Scripts/python.exe gap_report.py --text "Soma 0:03 ..."     # inline paste
  .venv/Scripts/python.exe gap_report.py --stdin < meeting.txt      # piped
  .venv/Scripts/python.exe gap_report.py --transcript meeting.txt --date 2026-07-28
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

from standup_summarizer import gap, transcript  # noqa: E402
from standup_summarizer.config import ReasoningConfig, SlackConfig  # noqa: E402
from standup_summarizer.engines import build_engine  # noqa: E402
from standup_summarizer.fetch import build_client  # noqa: E402

import build_attendance as att  # noqa: E402
import build_report as rep  # noqa: E402

SHEET_TAB = "Gap Report"
HEADERS = ["Developer", "Date", "In Slack", "In Meeting", "Assessment", "Gaps"]
HEADER_BG = "1F4E78"


# --------------------------------------------------------------------------
# Slack stand-ups for the day (roster short names, same folding as the report)
# --------------------------------------------------------------------------
def slack_standups(cfg: SlackConfig, day: dt.date) -> dict[str, str]:
    """Return {short_name: combined stand-up text} for `day`, roster order."""
    order, _roll, _status, _leaves = att.fetch_channel(cfg)
    name_map = att.roster_name_map(order)

    entries, _ = rep.fetch_standup_entries(cfg)
    tid = day.isoformat()
    parts: dict[str, list[str]] = {s: [] for s in order}
    for e in entries:
        if e["date"] != tid:
            continue
        short = att._short_of(e["developer"], name_map)
        parts.setdefault(short, []).append(e["text"])
    return {short: "\n".join(parts.get(short, [])).strip() for short in order}


# --------------------------------------------------------------------------
# Render
# --------------------------------------------------------------------------
def to_rows(records: list[dict], day: dt.date) -> list[list[str]]:
    tid = day.isoformat()
    rows = []
    for r in records:
        rows.append([
            r["developer"], tid,
            "yes" if r["in_slack"] else "no",
            "yes" if r["in_meeting"] else "no",
            r["assessment"],
            "; ".join(r["gaps"]),
        ])
    return rows


def compose(records: list[dict], day: dt.date, speakers: list[str]) -> str:
    lines = [f"*Stand-up Gap Report — {day.strftime('%d-%m-%Y')}*", ""]
    for r in records:
        lines.append(f"*{r['developer']}* — {r['assessment']}")
        for g in r["gaps"]:
            lines.append(f"• {g}")
        if r["assessment"] == "no update":
            lines.append("• No Slack stand-up and not heard in the meeting")
    lines += ["", f"_Meeting speakers detected: {', '.join(speakers) if speakers else 'none'}_"]
    return "\n".join(lines).rstrip()


# --------------------------------------------------------------------------
# Google Sheets push (single 'Gap Report' tab — snapshot of the latest run)
# --------------------------------------------------------------------------
def _rgb(hex_color: str) -> dict:
    r, g, b = (int(hex_color[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return {"red": r, "green": g, "blue": b}


def push_sheet(rows: list[list[str]]) -> str | None:
    key_path = Path(os.environ.get("GOOGLE_SA_KEY_PATH", "credentials/google_credentials.json"))
    if not key_path.is_absolute():
        key_path = ROOT / key_path
    spreadsheet_id = os.environ.get("SPREADSHEET_ID", "").strip()
    if not spreadsheet_id or not key_path.exists():
        print("Skipping Google Sheets push — SPREADSHEET_ID or service-account key not configured.")
        return None

    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(
        str(key_path), scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    sh = gspread.authorize(creds).open_by_key(spreadsheet_id)
    values = [HEADERS] + rows
    try:
        ws = sh.worksheet(SHEET_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=SHEET_TAB, rows=len(values) + 10, cols=len(HEADERS) + 1)
    ws.clear()
    ws.resize(rows=max(len(values) + 5, 10), cols=max(len(HEADERS) + 1, 8))
    ws.update(values=values, range_name="A1", value_input_option="RAW")
    try:
        ws.freeze(rows=1)
        last_col = chr(ord("A") + len(HEADERS) - 1)
        ws.format(f"A1:{last_col}1", {
            "textFormat": {"bold": True, "foregroundColor": _rgb("FFFFFF")},
            "backgroundColor": _rgb(HEADER_BG),
            "horizontalAlignment": "CENTER",
        })
    except Exception as exc:  # noqa: BLE001 — formatting must never fail the push
        print(f"  (formatting skipped: {exc})")
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"


def post_slack(cfg: SlackConfig, text: str) -> None:
    client = build_client(cfg.bot_token)
    try:
        resp = client.chat_postMessage(channel=cfg.channel_id, text=text)
        print(f"Posted gap report to {cfg.channel_id} (ts={resp.get('ts')}).")
    except Exception as exc:  # noqa: BLE001
        hint = ""
        if "missing_scope" in str(exc):
            hint = "  -> Add the 'chat:write' bot scope to the Slack app and reinstall it."
        elif "not_in_channel" in str(exc):
            hint = f"  -> Invite the bot into {cfg.channel_id}."
        print(f"Slack post failed: {exc}{hint}")


# --------------------------------------------------------------------------
def _read_transcript(args) -> str:
    if args.transcript:
        return transcript.load_text(path=args.transcript)
    if args.text is not None:
        return transcript.load_text(text=args.text)
    if args.stdin:
        return sys.stdin.read()
    raise SystemExit("Provide a transcript: --transcript FILE, --text \"...\", or --stdin")


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare Slack stand-ups vs the meeting transcript.")
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--transcript", metavar="FILE", help="Otter .txt export / transcript file")
    src.add_argument("--text", metavar="TEXT", help="transcript text inline")
    src.add_argument("--stdin", action="store_true", help="read the transcript from stdin")
    parser.add_argument("--date", help="YYYY-MM-DD (default today)")
    parser.add_argument("--dry-run", action="store_true", help="print only; do not push/post")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()

    raw = _read_transcript(args)
    segments = transcript.parse_transcript(raw)
    if not segments:
        raise SystemExit("Transcript is empty — nothing to compare.")

    cfg = SlackConfig.from_env()
    standups = slack_standups(cfg, day)
    if not standups:
        raise SystemExit("No roster / stand-up data found for the channel.")

    reasoning = ReasoningConfig.from_env()
    print(f"Comparing {len(standups)} developer(s) against the transcript "
          f"via backend={reasoning.backend} model={reasoning.model} ...")
    try:
        engine = build_engine(reasoning)
        records = gap.analyse(standups, transcript.render(segments), engine,
                              reasoning.max_retries, speakers=transcript.speakers(segments))
    except Exception as exc:  # noqa: BLE001
        print(f"\nReasoning engine unavailable — {type(exc).__name__}: {exc}")
        sys.exit(2)  # exit 2 = skipped (backend not configured), not a hard failure

    text = compose(records, day, transcript.speakers(segments))
    print("\n" + text)

    if args.dry_run:
        print("\nDry run — not pushing to Sheets or posting to Slack.")
        return

    url = push_sheet(to_rows(records, day))
    if url:
        print(f"Wrote '{SHEET_TAB}' tab -> {url}")
    post_slack(cfg, text)


if __name__ == "__main__":
    main()
