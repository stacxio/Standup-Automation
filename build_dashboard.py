"""Scorecard dashboard agent — read the fact table, write a self-contained page.

Reads the 'Scorecard Daily' tab that `build_scorecard.py` maintains and renders
it to one HTML file with no external references: no CDN, no fonts, no images.
It opens from disk, mails, and publishes as-is.

All the rendering lives in `standup_summarizer.dashboard`, which is pure; this
file only fetches and writes.

Run:
  .venv/Scripts/python.exe build_dashboard.py                 # -> Result/scorecard_dashboard.html
  .venv/Scripts/python.exe build_dashboard.py --out board.html
  .venv/Scripts/python.exe build_dashboard.py --demo          # sample data, no Sheets needed
  .venv/Scripts/python.exe build_dashboard.py --post          # + post the file to Slack
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

from standup_summarizer import dashboard, scorecard as sc  # noqa: E402
from standup_summarizer.config import SlackConfig  # noqa: E402
from standup_summarizer.fetch import build_client  # noqa: E402

import build_scorecard as bs  # noqa: E402

DEFAULT_OUT = ROOT / "Result" / "scorecard_dashboard.html"
DEMO_NOTE = ("Sample data — these are generated figures for design review, not "
             "anyone's real scores.")


def load_records() -> list[dict]:
    """The 'Scorecard Daily' fact table, parsed for the page."""
    sh, _url = bs.open_sheet()
    rows = bs.read_tab(sh, bs.DAILY_TAB)
    return dashboard.records_from_rows(rows, sc.DAILY_HEADERS)


def demo_records(*, days: int = 30, seed: int = 7) -> list[dict]:
    """Plausible sample data, for reviewing the layout before any real run.

    Deliberately uses placeholder names rather than the roster: a page of
    invented performance figures should never be mistakable for a real record
    of a real person.
    """
    rng = random.Random(seed)
    people = ["Developer A", "Developer B", "Developer C",
              "Developer D", "Developer E", "Developer F"]
    skill = {name: rng.uniform(0.45, 0.95) for name in people}
    today = dt.date.today()
    records: list[dict] = []

    for offset in range(days - 1, -1, -1):
        day = today - dt.timedelta(days=offset)
        if day.weekday() >= 5:
            continue  # weekends are not scored at all
        for name in people:
            roll = rng.random()
            if roll < 0.04:
                records.append(_demo_row(day, name, "Leave", sc.NOT_SCORED, reason="Leave"))
                continue
            if roll < 0.07:
                records.append(_demo_row(day, name, "Present", sc.NOT_SCORED,
                                         reason="incomplete jira facts: SP-14"))
                continue
            if roll < 0.11:
                records.append(_demo_row(day, name, "Absent", sc.SCORED,
                                         process=0.0, delivery=0.0, picked=0, done=0))
                continue

            half = roll < 0.15
            picked = rng.randint(1, 4)
            done = sum(1 for _ in range(picked) if rng.random() < skill[name])
            process = min(40.0, round(rng.uniform(0.6, 1.0) * 40 * skill[name] + 8, 1))
            delivery = round(60.0 * done / picked, 1)
            records.append(_demo_row(day, name, "Half Day" if half else "Present", sc.SCORED,
                                     process=process, delivery=delivery,
                                     picked=picked, done=done,
                                     flags=["half_day"] if half else []))
    return records


def _demo_row(day: dt.date, name: str, attendance: str, status: str, *,
              process: float | None = None, delivery: float | None = None,
              picked: int = 0, done: int = 0, reason: str = "",
              flags: list[str] | None = None) -> dict:
    total = None if status != sc.SCORED else round((process or 0) + (delivery or 0), 1)
    return {
        "date": day.isoformat(), "developer": name, "attendance": attendance,
        "status": status, "reason": reason, "picked": picked, "done": done,
        "process": process, "delivery": delivery, "total": total,
        "band": sc.band_of(total) if total is not None else "",
        "flags": flags or [],
    }


def post_to_slack(cfg: SlackConfig, path: Path) -> None:
    client = build_client(cfg.bot_token)
    try:
        client.files_upload_v2(
            channel=cfg.channel_id, file=str(path), filename=path.name,
            title="Team Scorecard", initial_comment="Scorecard dashboard updated.",
        )
        print(f"Posted {path.name} to {cfg.channel_id}.")
    except Exception as exc:  # noqa: BLE001
        hint = ""
        if "missing_scope" in str(exc):
            hint = "  -> add the 'files:write' bot scope and reinstall the app."
        print(f"Slack upload failed: {exc}{hint}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Render the scorecard dashboard.")
    parser.add_argument("--out", metavar="FILE", help=f"output path (default {DEFAULT_OUT})")
    parser.add_argument("--demo", action="store_true",
                        help="render sample data instead of reading Sheets")
    parser.add_argument("--days", type=int, default=30, help="--demo: days to generate")
    parser.add_argument("--post", action="store_true", help="upload the file to Slack")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    out = Path(args.out) if args.out else DEFAULT_OUT
    if not out.is_absolute():
        out = ROOT / out

    if args.demo:
        records, note = demo_records(days=args.days), DEMO_NOTE
    else:
        records, note = load_records(), ""
        if not records:
            print("No rows in the 'Scorecard Daily' tab yet — run build_scorecard.py --score,\n"
                  "or use --demo to preview the layout with sample data.")
            return

    html = dashboard.render(records, generated_at=dt.datetime.now(), note=note)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    developers = len({r["developer"] for r in records})
    print(f"Wrote {len(records)} record(s) for {developers} developer(s) -> {out}")

    if args.post:
        post_to_slack(SlackConfig.from_env(), out)


if __name__ == "__main__":
    main()
