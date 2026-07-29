"""Stand-up verification: compare each developer's Jira context vs the meeting.

For every roster developer it collects their active assigned Jira issues
(summary, status, priority, description/acceptance criteria, latest comments,
attachment names, pull requests), then asks the reasoning engine — acting as a
Scrum Master — whether the meeting stand-up actually covered them. It produces a
per-developer manager report (per-issue checks, coverage %, overall status,
recommendation), writes a 'Standup Verification' tab to the Daily Status sheet,
and posts a summary to #stacx-check-in (SLACK_CHANNEL_ID).

Transcript input is method A (Otter export / paste) — no Otter login or scrape.
Acceptance criteria are read from the issue description (this Jira has no
dedicated field). Attachments are matched by filename (no content extraction).

Run:
  .venv/Scripts/python.exe verify_standup.py --transcript meeting.txt
  .venv/Scripts/python.exe verify_standup.py --transcript meeting.txt --dry-run
  .venv/Scripts/python.exe verify_standup.py --stdin < meeting.txt
  .venv/Scripts/python.exe verify_standup.py --transcript m.txt --since-days 5 --max-issues 10
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

from standup_summarizer import jira, transcript, verify  # noqa: E402
from standup_summarizer.config import JiraConfig, ReasoningConfig, SlackConfig  # noqa: E402
from standup_summarizer.engines import build_engine  # noqa: E402
from standup_summarizer.fetch import build_client  # noqa: E402

import build_attendance as att  # noqa: E402
import build_report as rep  # noqa: E402

SHEET_TAB = "Standup Verification"
HEADERS = ["Developer", "Issues Reviewed", "Coverage", "Overall Status",
           "Not Discussed", "Additional Work", "Recommendation"]
HEADER_BG = "1F4E78"
MAX_COMMENTS = 5  # latest N comments passed to the engine per issue


# --------------------------------------------------------------------------
# Jira context collection
# --------------------------------------------------------------------------
def _norm(name: str) -> str:
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def _matches(a: str, b: str) -> bool:
    x, y = _norm(a), _norm(b)
    return bool(x and y) and (x.startswith(y) or y.startswith(x) or x in y or y in x)


def assignee_map(jira_cfg: JiraConfig, roster: list[str]) -> dict[str, str]:
    """Map roster short name -> Jira accountId, via active-issue assignees."""
    issues = jira.search_issues(
        jira_cfg, "statusCategory != Done ORDER BY updated DESC",
        fields="assignee", max_results=200,
    )
    accounts: dict[str, str] = {}  # displayName -> accountId
    for i in issues:
        a = (i.get("fields", {}) or {}).get("assignee") or {}
        if a.get("accountId") and a.get("displayName"):
            accounts.setdefault(a["displayName"], a["accountId"])

    mapping: dict[str, str] = {}
    for short in roster:
        for display, acct in accounts.items():
            if _matches(short, display):
                mapping[short] = acct
                break
    return mapping


def assigned_keys(jira_cfg: JiraConfig, account_id: str, since_days: int,
                  max_issues: int) -> list[str]:
    """Keys of the developer's active assigned issues, most-recently updated first."""
    jql = (f'assignee = "{account_id}" AND statusCategory != Done '
           f'AND updated >= -{since_days}d ORDER BY updated DESC')
    hits = jira.search_issues(jira_cfg, jql, fields="summary", max_results=max_issues)
    return [h["key"] for h in hits if h.get("key")]


def spoken_keys(segments: list[dict], roster: list[str]) -> dict[str, list[str]]:
    """Per developer, the Jira keys they named in their own transcript turns.

    Attributed by matching each segment's speaker label to a roster name, so an
    issue a developer actually discussed is verified even if it fell outside the
    assigned-issue window (and isn't mislabelled 'not in Jira').
    """
    out: dict[str, list[str]] = {s: [] for s in roster}
    for seg in segments:
        if not seg["speaker"]:
            continue
        dev = next((d for d in roster if _matches(d, seg["speaker"])), None)
        if not dev:
            continue
        for k in rep.extract_jira_ids(seg["text"]):
            if k not in out[dev]:
                out[dev].append(k)
    return out


def build_context(jira_cfg: JiraConfig, keys: list[str]) -> list[dict]:
    """Fetch the AI-facing context for each key; skip keys Jira can't resolve."""
    issues: list[dict] = []
    for key in keys:
        detail = jira.issue_detail(jira_cfg, key)
        if not detail:
            continue
        comments = jira.issue_comments(jira_cfg, key) or []
        dev = jira.issue_dev_info(jira_cfg, detail["id"]) if detail["id"] else {"pull_requests": []}
        issues.append({
            "key": detail["key"],
            "summary": detail["summary"],
            "status": detail["status_name"],
            "priority": detail["priority"],
            "description": detail["description"],
            "comments": [{"author": c["author"], "body": c["body"]}
                         for c in comments[:MAX_COMMENTS]],
            "attachments": detail["attachments"],
            "pull_requests": [{"url": p["url"], "status": p["status"]}
                              for p in dev.get("pull_requests", [])],
        })
    return issues


# --------------------------------------------------------------------------
# Render
# --------------------------------------------------------------------------
_TICK = {"yes": "✔", "partial": "⚠", "no": "✖", "na": "–"}


def _issue_line(chk: dict) -> list[str]:
    if not chk["discussed"]:
        return [f"  ✖ {chk['key']} — not discussed in the stand-up"]
    bits = [
        f"desc {_TICK[chk['description_reflected']]}",
        f"comments {_TICK[chk['comments_reflected']]}",
        f"AC {_TICK[chk['acceptance_progress']]}",
        f"PR {_TICK[chk['pr_mentioned']]}",
        f"attach {_TICK[chk['attachments_referenced']]}",
    ]
    if chk["blocker_mentioned"]:
        bits.append("blocker ⚠")
    lines = [f"  ✔ {chk['key']} — " + ", ".join(bits)]
    lines += [f"      • {n}" for n in chk["notes"]]
    return lines


def compose(records: list[dict], speakers: list[str]) -> str:
    lines = ["*Stand-up Verification Report*", ""]
    for r in records:
        reviewed = len(r["issues"])
        lines.append(f"*{r['developer']}* — {r['coverage']}% · {r['overall_status']} "
                     f"({reviewed} issue{'s' if reviewed != 1 else ''} reviewed)")
        for chk in r["issues"]:
            lines += _issue_line(chk)
        for w in r["additional_work"]:
            lines.append(f"  + Not in Jira: {w}")
        if r["recommendation"]:
            lines.append(f"  → {r['recommendation']}")
        lines.append("")
    lines.append(f"_Meeting speakers detected: {', '.join(speakers) if speakers else 'none'}_")
    return "\n".join(lines).rstrip()


def to_rows(records: list[dict]) -> list[list[str]]:
    rows = []
    for r in records:
        not_discussed = [c["key"] for c in r["issues"] if not c["discussed"]]
        rows.append([
            r["developer"], str(len(r["issues"])), f"{r['coverage']}%", r["overall_status"],
            ", ".join(not_discussed), "; ".join(r["additional_work"]), r["recommendation"],
        ])
    return rows


# --------------------------------------------------------------------------
# Google Sheets + Slack output
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
    except Exception as exc:  # noqa: BLE001
        print(f"  (formatting skipped: {exc})")
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"


def post_slack(cfg: SlackConfig, text: str) -> None:
    client = build_client(cfg.bot_token)
    try:
        resp = client.chat_postMessage(channel=cfg.channel_id, text=text)
        print(f"Posted verification report to {cfg.channel_id} (ts={resp.get('ts')}).")
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
    parser = argparse.ArgumentParser(description="Verify stand-up coverage of Jira issues vs the meeting.")
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--transcript", metavar="FILE", help="Otter .txt export / transcript file")
    src.add_argument("--text", metavar="TEXT", help="transcript text inline")
    src.add_argument("--stdin", action="store_true", help="read the transcript from stdin")
    parser.add_argument("--since-days", type=int, default=3, help="assigned-issue recency window (default 3)")
    parser.add_argument("--max-issues", type=int, default=8, help="max issues per developer (default 8)")
    parser.add_argument("--dry-run", action="store_true", help="print only; do not push/post")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    jira_cfg = JiraConfig.from_env()
    if not jira_cfg:
        sys.exit("Jira not configured — set JIRA_BASE_URL/JIRA_EMAIL/JIRA_API_TOKEN in .env.")

    raw = _read_transcript(args)
    segments = transcript.parse_transcript(raw)
    if not segments:
        raise SystemExit("Transcript is empty — nothing to verify.")
    transcript_text = transcript.render(segments)

    cfg = SlackConfig.from_env()
    roster, *_ = att.fetch_channel(cfg)
    accounts = assignee_map(jira_cfg, roster)
    mentioned = spoken_keys(segments, roster)
    print(f"Roster: {roster}")
    print(f"Matched Jira accounts: {sorted(accounts)}")

    reasoning = ReasoningConfig.from_env()
    engine = build_engine(reasoning)

    records = []
    for short in roster:
        acct = accounts.get(short)
        keys = assigned_keys(jira_cfg, acct, args.since_days, args.max_issues) if acct else []
        for k in mentioned.get(short, []):  # include issues they actually named
            if k not in keys:
                keys.append(k)
        issues = build_context(jira_cfg, keys)
        print(f"  {short}: {len(issues)} issue(s) -> {[i['key'] for i in issues]}")
        try:
            records.append(verify.verify_developer(short, issues, transcript_text, engine, reasoning.max_retries))
        except Exception as exc:  # noqa: BLE001
            print(f"\nReasoning engine unavailable — {type(exc).__name__}: {exc}")
            sys.exit(2)

    text = compose(records, transcript.speakers(segments))
    print("\n" + text)

    if args.dry_run:
        print("\nDry run — not pushing to Sheets or posting to Slack.")
        return

    url = push_sheet(to_rows(records))
    if url:
        print(f"Wrote '{SHEET_TAB}' tab -> {url}")
    post_slack(cfg, text)


if __name__ == "__main__":
    main()
