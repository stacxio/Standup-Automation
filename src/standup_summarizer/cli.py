"""Command-line entry point for smoke-testing the fetch stage against Slack.

Usage (from the project root, with .env populated):

    python -m standup_summarizer.cli fetch
    python -m standup_summarizer.cli fetch --date 2026-06-17 --tz Asia/Colombo
    python -m standup_summarizer.cli fetch --json

It computes the run window for a standup date, pulls and groups the channel's
messages (FR-1..FR-6), and prints a per-person view. Credentials come from the
environment / .env only — never the command line (NFR-5).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import SlackConfig
from .fetch import fetch_grouped_messages
from .models import GroupedMessages


def day_window(date: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """The full local day [00:00:00, 23:59:59.999999] for ``date`` in ``tz``.

    This is a simple stand-in for the run-window logic that run.py will own
    (FR-17); it's enough to exercise fetch against a real channel.
    """
    start = datetime.combine(date.date(), time.min, tzinfo=tz)
    end = start + timedelta(days=1) - timedelta(microseconds=1)
    return start, end


def _load_dotenv() -> None:
    """Load .env if python-dotenv is available; no-op otherwise."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def _print_human(grouped: GroupedMessages, start: datetime, end: datetime) -> None:
    window = f"{start:%Y-%m-%d %H:%M %z} -> {end:%Y-%m-%d %H:%M %z}"
    print(f"Run window: {window}")
    if not grouped:
        print("No standup messages found for this window.")
        return
    print(f"{len(grouped)} person(s) posted:\n")
    for person in grouped.values():
        print(f"  {person.display_name} ({person.user_id}) - {len(person.messages)} msg")
        for msg in person.messages:
            ts = datetime.fromtimestamp(msg.epoch, tz=start.tzinfo)
            tag = " [thread]" if msg.is_thread_reply else ""
            text = msg.text.replace("\n", " ")
            print(f"    {ts:%H:%M}{tag}  {text}")
        print()


def _print_json(grouped: GroupedMessages) -> None:
    payload = {
        uid: {
            "display_name": p.display_name,
            "messages": [
                {"ts": m.ts, "text": m.text, "is_thread_reply": m.is_thread_reply}
                for m in p.messages
            ],
        }
        for uid, p in grouped.items()
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _cmd_fetch(args: argparse.Namespace) -> int:
    _load_dotenv()

    try:
        tz = ZoneInfo(args.tz)
    except ZoneInfoNotFoundError:
        print(f"Unknown timezone: {args.tz}", file=sys.stderr)
        return 2

    if args.date:
        try:
            target = datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            print(f"Invalid --date (expected YYYY-MM-DD): {args.date}", file=sys.stderr)
            return 2
    else:
        target = datetime.now(tz)

    try:
        config = SlackConfig.from_env()
    except RuntimeError as exc:
        print(f"{exc}\nFill SLACK_BOT_TOKEN and SLACK_CHANNEL_ID in your .env.", file=sys.stderr)
        return 2

    start, end = day_window(target, tz)

    try:
        grouped = fetch_grouped_messages(config, start, end)
    except Exception as exc:  # noqa: BLE001 — surface a clean message, no token
        print(f"Slack fetch failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.json:
        _print_json(grouped)
    else:
        _print_human(grouped, start, end)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="standup_summarizer", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    fetch_p = sub.add_parser("fetch", help="Fetch and group a day's standup messages")
    fetch_p.add_argument("--date", help="Standup date YYYY-MM-DD (default: today)")
    fetch_p.add_argument("--tz", default="UTC", help="IANA timezone (default: UTC)")
    fetch_p.add_argument("--json", action="store_true", help="Emit raw JSON")
    fetch_p.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    fetch_p.set_defaults(func=_cmd_fetch)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
