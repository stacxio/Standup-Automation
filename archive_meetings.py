"""Meeting archive agent: file Otter meetings into Drive, organised by project.

Pulls meetings from Otter (Public API, the web API, or a folder of hand
exports), works out which project each one belongs to from the Jira issue keys
spoken in it, and files the recording, transcript, AI summary, discussion notes
and a meta.json into a per-meeting folder in Google Drive:

    <DRIVE_ARCHIVE_FOLDER>/<Project>/<Year>/<YYYY-MM-DD>_<slug>/

It then upserts a row per meeting into the 'Meeting Archive' tab of the Daily
Status sheet (the browsable history) and posts the links to that project's
Slack channel — the same channel the daily summary already routes to.

Re-runs are idempotent: a meeting folder that already holds meta.json is
skipped unless you pass --force, and notes.md is never overwritten once it
exists, so the team's edits are safe.

Run:
  .venv/Scripts/python.exe archive_meetings.py                     # today
  .venv/Scripts/python.exe archive_meetings.py --since-days 7      # last week
  .venv/Scripts/python.exe archive_meetings.py --date 2026-08-05
  .venv/Scripts/python.exe archive_meetings.py --project HIROCOM   # force project
  .venv/Scripts/python.exe archive_meetings.py --inbox exports/    # no Otter login
  .venv/Scripts/python.exe archive_meetings.py --dry-run           # plan only
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

from standup_summarizer import archive, otter  # noqa: E402
from standup_summarizer.config import ArchiveConfig, OtterConfig, SlackConfig  # noqa: E402
from standup_summarizer.fetch import build_client  # noqa: E402

import build_report as rep  # noqa: E402

SHEET_TAB = "Meeting Archive"
HEADER_BG = "1F4E78"

MIME = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".json": "application/json",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".webm": "audio/webm",
    ".mp4": "video/mp4",
}


# --------------------------------------------------------------------------
# Jira keys spoken in a meeting
# --------------------------------------------------------------------------
def extract_keys(text: str, prefixes: set[str]) -> list[str]:
    """Jira keys named in `text`, ordered by where they were spoken.

    Two passes. Known ARCHIVE_PROJECT_NAMES prefixes are matched tolerantly,
    because people say issue ids out loud without the hyphen ("SP 19", "HIR7")
    and Otter transcribes them that way — the same tolerance the verification
    agent applies, but driven from config so no Jira call is needed just to
    decide which folder a meeting belongs in. Anything else falls back to the
    report agent's stricter hyphenated matcher, so an unmapped board is still
    recorded on the meeting.

    Order is by position in the text, deliberately: `prefixes` is a set, so
    iterating it would order keys differently from run to run, and
    detect_project() breaks ties on first mention. A stable order is what keeps
    a re-run filing the same meeting under the same project.
    """
    text = text or ""
    at: dict[str, int] = {}
    for prefix in sorted(prefixes):
        for match in re.finditer(rf"\b{re.escape(prefix)}\s*-?\s*0*(\d+)\b", text, re.I):
            at.setdefault(f"{prefix}-{int(match.group(1))}", match.start())
    for key in rep.extract_jira_ids(text):
        if key in at:
            continue
        prefix, number = key.split("-", 1)
        found = re.search(rf"\b{re.escape(prefix)}[ \t]*-[ \t]*0*{number}\b", text, re.I)
        at[key] = found.start() if found else len(text)
    return sorted(at, key=lambda k: (at[k], k))


def channel_for_project(slack_cfg: SlackConfig, arch_cfg: ArchiveConfig, project: str) -> str:
    """Slack channel for an archive project, via its issue-key prefixes.

    ARCHIVE_PROJECT_NAMES maps prefix -> project and SUMMARY_CHANNEL_ROUTES maps
    prefix -> channel, so the archive reuses the routing the daily summary
    already uses instead of introducing a second mapping to keep in sync.
    """
    for prefix, name in arch_cfg.project_names.items():
        if name == project and slack_cfg.channel_routes.get(prefix):
            return slack_cfg.channel_routes[prefix]
    return slack_cfg.channel_id


# --------------------------------------------------------------------------
# Drive
# --------------------------------------------------------------------------
def drive_service():
    import drive_oauth

    return drive_oauth, drive_oauth.get_service()


def archive_meeting(drive, svc, root_id: str, meeting, project: str, keys: list[str],
                    projects: list[str], source, *, keep_audio: bool, force: bool) -> dict | None:
    """Upload one meeting's artifacts; return its meta, or None if skipped."""
    segments = archive.folder_segments(project, meeting.date, meeting.title)
    folder_id = drive.ensure_path(svc, segments, root_id)
    where = "/".join(segments)

    if not force and drive.find_file(svc, archive.META, folder_id):
        print(f"  = {where} — already archived (use --force to refresh)")
        return None

    links: dict[str, dict] = {"folder": {"name": segments[-1], "url": drive.file_link(svc, folder_id)}}

    def put(name: str, data: bytes, *, keep_existing: bool = False) -> None:
        existing = drive.find_file(svc, name, folder_id) if keep_existing else None
        if existing:
            links[name] = {"name": name, "url": drive.file_link(svc, existing)}
            print(f"    · {name} kept (already edited)")
            return
        mime = MIME.get(Path(name).suffix.lower(), "application/octet-stream")
        file_id, action = drive.upsert_bytes(svc, name, data, folder_id, mime)
        links[name] = {"name": name, "url": drive.file_link(svc, file_id)}
        print(f"    · {name} {action} ({len(data):,} bytes)")

    put(archive.TRANSCRIPT, (meeting.transcript or "").encode("utf-8"))
    put(archive.SUMMARY, archive.summary_doc(meeting, project, keys).encode("utf-8"))
    # notes.md is the team's document — write the stub once, never clobber edits.
    put(archive.NOTES, archive.notes_doc(meeting, project, keys).encode("utf-8"),
        keep_existing=True)

    if keep_audio:
        audio = source.download_audio(meeting)
        if audio:
            data, ext = audio
            put(f"{archive.RECORDING}.{ext}", data)
            links[archive.RECORDING] = links.pop(f"{archive.RECORDING}.{ext}")

    # meta.json is written last: it doubles as the "already archived" marker, so
    # it must only appear once every other artifact is safely uploaded.
    meta = archive.build_meta(meeting, project, keys, dict(links), projects=projects,
                              archived_at=dt.datetime.now().astimezone())
    put(archive.META, json.dumps(meta, indent=2, ensure_ascii=False).encode("utf-8"))
    print(f"  + {where}")
    return meta


# --------------------------------------------------------------------------
# Google Sheets index ('Meeting Archive' tab — accumulates, upserts by id)
# --------------------------------------------------------------------------
def _rgb(hex_color: str) -> dict:
    r, g, b = (int(hex_color[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return {"red": r, "green": g, "blue": b}


def push_sheet(metas: list[dict]) -> str | None:
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
    headers = archive.INDEX_HEADERS
    try:
        ws = sh.worksheet(SHEET_TAB)
        existing = ws.get_all_values()[1:]  # drop the header row
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=SHEET_TAB, rows=50, cols=len(headers) + 1)
        existing = []

    rows = archive.merge_index(existing, [archive.index_row(m) for m in metas])
    values = [headers] + rows
    ws.clear()
    ws.resize(rows=max(len(values) + 5, 10), cols=max(len(headers) + 1, 8))
    ws.update(values=values, range_name="A1", value_input_option="RAW")
    try:
        ws.freeze(rows=1)
        last_col = chr(ord("A") + len(headers) - 1)
        ws.format(f"A1:{last_col}1", {
            "textFormat": {"bold": True, "foregroundColor": _rgb("FFFFFF")},
            "backgroundColor": _rgb(HEADER_BG),
            "horizontalAlignment": "CENTER",
        })
    except Exception as exc:  # noqa: BLE001 — formatting must never fail the push
        print(f"  (formatting skipped: {exc})")
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"


def post_slack(cfg: SlackConfig, channel: str, text: str) -> None:
    client = build_client(cfg.bot_token)
    try:
        resp = client.chat_postMessage(channel=channel, text=text, unfurl_links=False)
        print(f"Posted archive notice to {channel} (ts={resp.get('ts')}).")
    except Exception as exc:  # noqa: BLE001
        hint = ""
        if "missing_scope" in str(exc):
            hint = "  -> Add the 'chat:write' bot scope to the Slack app and reinstall it."
        elif "not_in_channel" in str(exc):
            hint = f"  -> Invite the bot into {channel}."
        print(f"Slack post failed: {exc}{hint}")


# --------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Archive Otter meetings (recording, transcript, summary, notes) to Drive by project."
    )
    when = parser.add_mutually_exclusive_group()
    when.add_argument("--date", help="archive a single day, YYYY-MM-DD")
    when.add_argument("--since-days", type=int, default=1,
                      help="archive the last N days including today (default 1)")
    parser.add_argument("--project", help="force the project folder instead of detecting it")
    parser.add_argument("--inbox", metavar="DIR",
                        help="archive Otter files exported by hand from DIR (no Otter login)")
    parser.add_argument("--limit", type=int, default=25, help="max meetings per run (default 25)")
    parser.add_argument("--no-audio", action="store_true", help="skip recordings (text only)")
    parser.add_argument("--force", action="store_true", help="re-archive meetings already filed")
    parser.add_argument("--dry-run", action="store_true", help="print the plan; upload/post nothing")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    arch_cfg = ArchiveConfig.from_env()

    if args.date:
        since = until = dt.date.fromisoformat(args.date)
    else:
        until = dt.date.today()
        since = until - dt.timedelta(days=max(args.since_days, 1) - 1)

    otter_cfg = OtterConfig.from_env()
    inbox = Path(args.inbox) if args.inbox else None
    source = otter.build_source(otter_cfg, inbox)
    if source is None:
        print(
            "No meeting source configured. Either:\n"
            "  * set OTTER_API_KEY (Otter Public API — Enterprise workspaces), or\n"
            "  * set OTTER_BACKEND=web with OTTER_EMAIL/OTTER_PASSWORD, or\n"
            "  * export from Otter by hand and run with --inbox DIR."
        )
        sys.exit(2)  # exit 2 = skipped (not configured), matching the other agents

    print(f"Source: {source.name} | window {since} .. {until} | limit {args.limit}")
    prefixes = {p.upper() for p in arch_cfg.project_names}
    stubs = source.list_meetings(since, until, args.limit)
    if not stubs:
        print("No meetings found in that window — nothing to archive.")
        return

    print(f"Found {len(stubs)} meeting(s).")
    plans = []
    for stub in stubs:
        meeting = source.fetch(stub)
        haystack = "\n".join([meeting.title, meeting.transcript, meeting.summary,
                              *meeting.action_items])
        keys = extract_keys(haystack, prefixes)
        detected, mentioned = archive.detect_project(keys, arch_cfg.project_names,
                                                     arch_cfg.default_project)
        project = args.project or detected
        plans.append((meeting, project, keys, mentioned))
        extra = f" (also: {', '.join(p for p in mentioned if p != project)})" if len(mentioned) > 1 else ""
        print(f"  {meeting.date} {meeting.title!r} -> {project}{extra} "
              f"| {len(keys)} issue(s) | {meeting.duration_hms}")

    if args.dry_run:
        for meeting, project, keys, _ in plans:
            path = "/".join([arch_cfg.root_name] + archive.folder_segments(
                project, meeting.date, meeting.title))
            print(f"\n--- {path}")
            print(f"    {archive.TRANSCRIPT} ({len(meeting.transcript):,} chars)")
            print(f"    {archive.SUMMARY}, {archive.NOTES}, {archive.META}")
            if not args.no_audio and arch_cfg.keep_audio:
                print(f"    {archive.RECORDING}.{meeting.audio_ext}")
        print("\nDry run — nothing uploaded, no sheet write, no Slack post.")
        return

    drive, svc = drive_service()
    root_id = arch_cfg.root_folder_id or drive.find_or_create_folder(svc, arch_cfg.root_name)
    keep_audio = arch_cfg.keep_audio and not args.no_audio

    metas: list[dict] = []
    for meeting, project, keys, mentioned in plans:
        meta = archive_meeting(drive, svc, root_id, meeting, project, keys, mentioned, source,
                               keep_audio=keep_audio, force=args.force)
        if meta:
            metas.append(meta)

    if not metas:
        print("\nEverything in this window was already archived.")
        return

    url = push_sheet(metas)
    if url:
        print(f"Wrote '{SHEET_TAB}' tab -> {url}")

    # Slack config is read here, not at start-up, so --dry-run and --inbox work
    # on a machine with no Slack token configured.
    slack_cfg = SlackConfig.from_env()
    by_project: dict[str, list[dict]] = {}
    for meta in metas:
        by_project.setdefault(meta["project"], []).append(meta)
    for project, group in by_project.items():
        post_slack(slack_cfg, channel_for_project(slack_cfg, arch_cfg, project),
                   archive.compose_slack(group, project))


if __name__ == "__main__":
    main()
