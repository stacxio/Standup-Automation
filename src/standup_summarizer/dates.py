"""Parsing the run date, shared by the orchestrator and the agents it calls.

`run_daily.py` takes a date from a person, who writes it the way the team writes
dates elsewhere in this project (19-08-2026); the agents take one from
`run_daily` and from scripts that already use ISO. Both forms are accepted
everywhere so a date never has to be reformatted between the command line and
the subprocess that receives it.

Kept in one place because a date parsed differently by two steps would run them
against different days without failing — the worst kind of bug for a job nobody
watches.
"""

from __future__ import annotations

import datetime as dt

# Day-first is the human form used in the digest header and the payslips;
# ISO is what `date.fromisoformat` and the sheet columns use.
_FORMATS = ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d")


def parse_day(text: str) -> dt.date:
    """Return the date named by `text`, or raise ValueError with the accepted forms.

    "19-08-2026" and "2026-08-19" both mean 19 August 2026. The two cannot be
    confused: a four-digit year is unambiguous at either end.
    """
    value = str(text or "").strip()
    for fmt in _FORMATS:
        try:
            return dt.datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError(
        f"unrecognised date {text!r} — use DD-MM-YYYY (19-08-2026) "
        f"or YYYY-MM-DD (2026-08-19)"
    )


def day_from_argv(argv: list[str], *, default: dt.date | None = None) -> dt.date:
    """Read `--date VALUE` or `--date=VALUE` out of `argv`.

    The agents parse their arguments by scanning argv rather than with argparse,
    and tolerate flags they do not recognise — several are invoked with a mix of
    `--notify`, `--dry-run` and now `--date`. This keeps that contract.
    """
    for index, arg in enumerate(argv):
        if arg == "--date" and index + 1 < len(argv):
            return parse_day(argv[index + 1])
        if arg.startswith("--date="):
            return parse_day(arg.split("=", 1)[1])
    return default or dt.date.today()
