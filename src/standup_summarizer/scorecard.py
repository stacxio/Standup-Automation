"""Daily performance scoring — the rubric as a pure function.

Implements docs/SCORING.md. Like `archive.py`, this module is deliberately
pure: no Slack, Jira, Sheets or clock calls, just the arithmetic that turns a
day's facts into a score. `build_scorecard.py` supplies the I/O.

The rubric, out of 100:

    process   check-in 10 | task picked  5 | description 10
              commit    5 | comment     10                     = 40 (0-40)
    delivery  task Done, or In Review with a commit id in a
              comment                                          = 60 (0-60)

Checks 1-2 are per day and binary; 3-6 are evaluated per task and averaged
across the tasks the developer picked, which is what produces partial credit.

Two properties everything here is built around:

  * **Reproducible** — a score is a pure function of its inputs, so the nightly
    recompute (SCORING.md §7.2) cannot drift. `computed_at` is a parameter and
    never read from the clock, the same convention `archive.build_meta` uses
    for `archived_at`. The reasoning engine is not involved at any point.
  * **Explainable** — every record carries the evidence that produced it, per
    task and per check, including *why* a check failed. That column is what
    stands in for human review; there is no manual override.

A missing fact is never a low score: where data is absent or partial the day is
`Not Scored` and excluded from averages, which is a different value from the
real `0` an absent developer earns.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass

SCHEMA = 1

# The 'Scorecard Daily' tab on the Daily Status sheet — the append-only fact
# table the dashboard reads (SCORING.md §8.1).
DAILY_HEADERS = [
    "Date", "Developer", "Attendance", "Status", "Reason",
    "Picked Tasks", "Tasks Picked", "Tasks Done", "Task Credit",
    "Check-in", "Task Picked", "Description", "Commit", "Comment", "Done",
    "Process", "Delivery", "Volume Factor", "Total", "Band", "Flags",
    "Evidence", "Computed At",
]
DATE_COLUMN = DAILY_HEADERS.index("Date")
DEVELOPER_COLUMN = DAILY_HEADERS.index("Developer")

SCORED = "Scored"
NOT_SCORED = "Not Scored"

# Attendance that means "there was no working day to score" — excluded from
# averages entirely rather than counted as zero, so that approved leave never
# reads as non-performance (SCORING.md §2).
UNSCORABLE_ATTENDANCE = frozenset({"leave", "weekend", "holiday"})
HALF_DAY = "half day"

BANDS = ((85.0, "Excellent"), (70.0, "On Track"), (50.0, "Needs Attention"), (0.0, "At Risk"))

# A commit reference in free text, in three forms.
#   1. a commit URL path, whatever it is labelled:  .../commit/9f8e7d6
_COMMIT_URL = re.compile(r"/commits?/[0-9a-fA-F]{7,40}")
#   2. a bare sha:  "merged 2934bb5"
_SHA = re.compile(r"(?<![\w-])[0-9a-fA-F]{7,40}(?![\w-])")
#   3. a commit *label* followed by a url, which is how the team actually writes
#      it: "Latest commit: <url>", "commit id - <url>", "commits <url>". The
#      url need not contain /commit/ — a compare link, a tree link or a
#      shortened link all count once the developer has named it as the commit.
#      Only whitespace and an optional separator may sit between the label and
#      the url, so "we commit to ship this: https://plan" is not a commit.
_LABELLED_COMMIT_URL = re.compile(
    r"(?:latest\s+)?commits?(?:\s*ids?)?\s*[:\-–]?\s*(https?://\S+)", re.I
)
# Punctuation a url picks up when it ends a sentence or sits in brackets.
_URL_TRAILING = ".,;:!?)]}>\"'"


def _norm(text: str) -> str:
    """Case-folded, whitespace-collapsed — Jira workflow names are hand-edited."""
    return " ".join(str(text or "").split()).lower()


def _normset(values) -> set[str]:
    return {_norm(v) for v in (values or ()) if _norm(v)}


def _fingerprint(text: str) -> str:
    """Stable hash of a comment body, ignoring case and whitespace.

    Used to spot a comment pasted again verbatim; normalising first means
    re-indenting or re-casing the same line does not evade the check.
    """
    return hashlib.sha1(_norm(text).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Inputs — facts the caller has already fetched
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class CommentFacts:
    """One Jira comment. The rules that judge it live in this module."""

    body: str
    created: dt.date
    authored_by_developer: bool = False
    """Resolved by the caller against the Master roster — identity is not a
    scoring rule, and keeping it out means this module never needs the roster."""
    media: tuple[str, ...] = ()
    """Filenames of images attached to the comment. A screenshot-only comment
    has an empty body but is still a real update of the ticket."""


@dataclass(frozen=True)
class TaskFacts:
    """One picked Jira issue, as it stood at the cutoff."""

    key: str
    description: str = ""
    """Plain text; the caller has already run `jira.adf_to_text`."""
    parent_description: str = ""
    """The parent issue's description, for a sub-task that has none of its own."""
    status: str = ""
    status_category: str = ""
    issue_type: str = ""
    has_linked_commit: bool = False
    """From the Jira development panel — the authoritative source. Text in a
    comment deliberately does not satisfy this check: a bare hex pattern can be
    typed, a linked commit cannot."""
    comments: tuple[CommentFacts, ...] = ()
    url: str = ""


@dataclass(frozen=True)
class DayFacts:
    """Everything needed to score one developer for one day."""

    developer: str
    date: dt.date
    attendance: str = "Present"
    checked_in: bool = False
    picked_tasks: tuple[str, ...] = ()
    """Frozen at capture time so tasks cannot be dropped from a commitment
    during the day (SCORING.md §3)."""
    tasks: tuple[TaskFacts, ...] = ()
    median_picked: float | None = None
    """The developer's own trailing-30-day median; None when history is thin."""
    data_ok: bool = True
    data_error: str = ""


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Weights:
    """The six weights. Configuration, not constants — they will be tuned."""

    checkin: float = 10.0
    picked: float = 5.0
    description: float = 10.0
    commit: float = 5.0
    comment: float = 10.0
    done: float = 60.0

    @property
    def process_max(self) -> float:
        return self.checkin + self.picked + self.description + self.commit + self.comment

    def validate(self) -> None:
        """Raise unless the six weights sum to 100.

        Checked before every score rather than trusted, so a mistyped `.env`
        fails loudly instead of quietly scoring everyone out of 95.
        """
        total = self.process_max + self.done
        if abs(total - 100.0) > 1e-9:
            raise ValueError(f"scoring weights must sum to 100, got {total:g}")


@dataclass(frozen=True)
class Thresholds:
    """Where each check draws its line (SCORING.md §10)."""

    min_description_chars: int = 30
    min_comment_chars: int = 20
    review_statuses: frozenset[str] = frozenset({"In Review", "Code Review", "Review"})
    incident_types: frozenset[str] = frozenset({"Incident", "Support"})
    incident_projects: frozenset[str] = frozenset()
    min_median_tasks: float = 2.0

    parent_description_fallback: bool = True
    """Let a sub-task inherit its parent's description. Sub-tasks routinely have
    none of their own — the context lives one level up — so without this the
    description check is unreachable for teams that break work down that way."""

    media_counts_as_comment: bool = True
    """Count a screenshot-only comment as a ticket update. It has no text, but
    posting evidence of progress is the behaviour the check exists to reward."""

    commit_in_comment_counts: bool = True
    """Accept a commit id written in a comment for the commit check, not only a
    link in the development panel. The panel stays authoritative and is
    recorded as the source when present; this fallback is what keeps the check
    reachable at all when no SCM is connected to Jira."""

    # Delivery credit per task state. Binary done/not-done scores a ticket
    # sitting in review the same as one never started, which is wrong in both
    # directions. Every value is configurable because the right ladder depends
    # on how a team uses its workflow.
    credit_done: float = 1.0
    credit_review_with_commit: float = 1.0
    credit_review: float = 0.5
    credit_in_progress: float = 0.25
    credit_todo: float = 0.0


# --------------------------------------------------------------------------
# Individual rules — each independently testable
# --------------------------------------------------------------------------
def commit_ids_in_text(text: str) -> list[str]:
    """The commit ids named in free text, in order, deduplicated.

    A commit URL path counts outright. A bare token counts only if it is 7-40
    hex characters, standalone, and contains **both** a digit and a letter a-f:
    without that last condition `deadbeef`, `facade` and `accede` all read as
    commits, and a false positive here hands out the full 60-point delivery
    score on an In Review ticket. The lookbehind also keeps the numeric half of
    a Jira key (`SP-1234567`) from matching.

    A url the developer has explicitly labelled as the commit counts too, even
    when its path is not `/commit/<sha>` — a compare link or a shortened link is
    still them telling us where the commit is.

    Returns the ids rather than a bare yes/no so callers can show what was found
    instead of only whether anything exists.
    """
    body = str(text or "")
    found: dict[str, None] = {}

    # A url whose own path names a commit — the strongest form.
    commit_urls = set()
    for url in _COMMIT_URL.findall(body):
        commit_urls.add(url)
        found.setdefault(url.rsplit("/", 1)[-1], None)

    # A url the developer labelled as the commit. Skip ones already counted
    # above, or a single commit would be reported twice.
    for url in _LABELLED_COMMIT_URL.findall(body):
        url = url.rstrip(_URL_TRAILING)
        if any(seen in url for seen in commit_urls):
            continue
        found.setdefault(url, None)

    for token in _SHA.findall(body):
        low = token.lower()
        if any(c.isdigit() for c in low) and any(c in "abcdef" for c in low):
            found.setdefault(token, None)

    return _drop_abbreviated(list(found))


def _drop_abbreviated(ids: list[str]) -> list[str]:
    """Remove a short sha that only abbreviates a longer one already listed.

    Developers routinely write both forms of the same commit in one comment —
    "b4eda6544adcba92984f2c101ee6fe6db81793b5 ... b4eda65" — which would
    otherwise be reported as two commits and inflate the count. The longer form
    is kept, since it is the one that identifies the commit unambiguously.
    """
    def is_sha(value: str) -> bool:
        return bool(_SHA.fullmatch(value))

    keep = []
    for candidate in ids:
        abbreviates_another = any(
            other != candidate
            and is_sha(candidate) and is_sha(other)
            and len(other) > len(candidate)
            and other.lower().startswith(candidate.lower())
            for other in ids
        )
        if not abbreviates_another:
            keep.append(candidate)
    return keep


def has_commit_reference(text: str) -> bool:
    """Whether free text names a git commit (SCORING.md §4.6)."""
    return bool(commit_ids_in_text(text))


def description_check(task: TaskFacts, thresholds: Thresholds) -> dict:
    """§4.3 — the ticket is documented. Who wrote it is irrelevant.

    A sub-task falls back to its parent's description: the work is documented,
    just one level up, and failing it for that is measuring the issue hierarchy
    rather than whether anyone can tell what the work is.
    """
    chars = len(str(task.description or "").strip())
    if chars >= thresholds.min_description_chars:
        return {"passed": True, "chars": chars, "source": "issue"}

    if thresholds.parent_description_fallback:
        inherited = len(str(task.parent_description or "").strip())
        if inherited >= thresholds.min_description_chars:
            return {"passed": True, "chars": inherited, "source": "parent"}

    return {"passed": False, "chars": chars, "source": "issue"}


def commit_check(task: TaskFacts, thresholds: Thresholds) -> dict:
    """§4.4 — a commit is linked in the development panel, or named in a comment.

    The panel is authoritative and cannot be faked by typing, so it is reported
    as the source whenever it has data. The comment fallback exists because a
    Jira with no SCM integration makes the panel permanently empty, and a check
    nobody can ever pass is not a measurement.
    """
    if task.has_linked_commit:
        return {"passed": True, "source": "dev_panel"}
    if thresholds.commit_in_comment_counts:
        if any(has_commit_reference(c.body) for c in task.comments):
            return {"passed": True, "source": "comment"}
    return {"passed": False, "source": ""}


def comment_check(task: TaskFacts, day: dt.date, thresholds: Thresholds) -> dict:
    """§4.5 — theirs, today, substantial, and not a repeat.

    A screenshot with no text counts: it carries no characters but it is a real
    update of the ticket. Everything else still applies, because without it
    pasting "working on it" into every picked ticket each morning would be a
    guaranteed 10 points a day.
    """
    theirs = [c for c in task.comments if c.authored_by_developer]
    today = [c for c in theirs if c.created == day]
    if not today:
        return {"passed": False, "reason": "no comment by them on the day"}

    seen_before = {_fingerprint(c.body) for c in theirs if c.created < day}
    longest, duplicate = 0, False
    for comment in today:
        chars = len(str(comment.body or "").strip())
        longest = max(longest, chars)
        if thresholds.media_counts_as_comment and comment.media:
            return {"passed": True, "chars": chars, "source": "media",
                    "media": list(comment.media), "created": comment.created.isoformat()}
        if chars < thresholds.min_comment_chars:
            continue
        if _fingerprint(comment.body) in seen_before:
            duplicate = True
            continue
        return {"passed": True, "chars": chars, "source": "text",
                "created": comment.created.isoformat()}

    reason = "duplicate of an earlier comment" if duplicate else "too short"
    return {"passed": False, "reason": reason, "chars": longest}


def task_credit(task: TaskFacts, thresholds: Thresholds) -> tuple[float, str]:
    """§4.6 -> (credit 0.0-1.0, rule) for one task's delivery.

    A ladder rather than a yes/no. Binary scoring pays nothing for a ticket
    sitting in review with the work finished, which is indistinguishable from
    never having started it — and that is exactly the state most in-flight work
    is in at a 6pm cutoff.
    """
    if _norm(task.status_category) == "done":
        return thresholds.credit_done, "done"
    if _norm(task.status) in _normset(thresholds.review_statuses):
        if any(has_commit_reference(c.body) for c in task.comments):
            return thresholds.credit_review_with_commit, "review_with_commit"
        return thresholds.credit_review, "review"
    if _norm(task.status_category) == "in progress":
        return thresholds.credit_in_progress, "in_progress"
    return thresholds.credit_todo, "todo"


def task_is_done(task: TaskFacts, thresholds: Thresholds) -> tuple[bool, str]:
    """Whether a task counts as fully delivered (credit 1.0)."""
    credit, rule = task_credit(task, thresholds)
    return credit >= 1.0, rule if credit >= 1.0 else ""


def is_incident(task: TaskFacts, thresholds: Thresholds) -> bool:
    """§7.1 — unplanned work, detected from the Jira issue type or project."""
    if _norm(task.issue_type) in _normset(thresholds.incident_types):
        return True
    prefix = str(task.key or "").split("-", 1)[0]
    return _norm(prefix) in _normset(thresholds.incident_projects)


def volume_factor(n: int, median: float | None, min_median: float) -> float:
    """§5.1 — scale delivery down when someone commits below their own baseline.

    `tasks_done / tasks_picked` alone pays the same 60 points for finishing one
    easy task as for finishing five. The comparison is against the developer's
    own trailing median, never against teammates, so it needs no target from a
    manager and creates no ranking. It can only reduce a score, and it stays
    dormant until there is enough history to be fair.
    """
    if median is None or median < min_median or median <= 0:
        return 1.0
    return min(1.0, n / float(median))


def band_of(total: float) -> str:
    """§6 — presentation only; nothing in the pipeline branches on the band."""
    for floor, name in BANDS:
        if total >= floor:
            return name
    return BANDS[-1][1]


# --------------------------------------------------------------------------
# The scorer
# --------------------------------------------------------------------------
def _round(value: float) -> float:
    return round(float(value), 1)


def _blank_points() -> dict:
    return {"checkin": 0.0, "picked": 0.0, "description": 0.0,
            "commit": 0.0, "comment": 0.0, "done": 0.0}


def _base(facts: DayFacts, computed_at: dt.datetime) -> dict:
    return {
        "developer": facts.developer,
        "date": facts.date.isoformat(),
        "computed_at": computed_at.isoformat(timespec="seconds"),
        "schema": SCHEMA,
        "attendance": facts.attendance,
        "status": SCORED,
        "reason": "",
        "picked_tasks": list(facts.picked_tasks),
        "tasks_picked": len(facts.picked_tasks),
        "tasks_done": 0,
        "tasks_credit": 0.0,
        "points": _blank_points(),
        "process": 0.0,
        "delivery": 0.0,
        "volume_factor": 1.0,
        "total": 0.0,
        "band": "",
        "flags": [],
        "evidence": {"checkin": {"passed": False}, "picked": {"passed": False, "count": 0},
                     "tasks": []},
    }


def _not_scored(record: dict, reason: str) -> dict:
    """A fact was missing, not bad — excluded from averages, never a zero."""
    record["status"] = NOT_SCORED
    record["reason"] = reason
    record["total"] = None
    record["band"] = ""
    return record


def score_day(facts: DayFacts, weights: Weights, thresholds: Thresholds,
              *, computed_at: dt.datetime) -> dict:
    """Score one developer for one day (SCORING.md §4-§5).

    The decision order matters and is checked here in full: unusable data and
    partial data both yield `Not Scored`, and only a genuine absence yields 0.
    """
    weights.validate()
    record = _base(facts, computed_at)

    if not facts.data_ok:
        return _not_scored(record, facts.data_error or "data unavailable")

    known = {t.key for t in facts.tasks}
    missing = [k for k in facts.picked_tasks if k not in known]
    if missing:
        # Scoring the tasks that did arrive can only understate the developer —
        # the absent ones could only have added points. Partial data is a data
        # failure; the recompute pass retries it.
        return _not_scored(record, f"incomplete jira facts: {', '.join(missing)}")

    attendance = _norm(facts.attendance)
    if attendance in UNSCORABLE_ATTENDANCE:
        return _not_scored(record, facts.attendance)
    if attendance == "absent":
        record["band"] = band_of(0.0)
        return record

    by_key = {t.key: t for t in facts.tasks}
    ordered = [by_key[k] for k in facts.picked_tasks]
    n = len(ordered)

    per_task = []
    for task in ordered:
        credit, rule = task_credit(task, thresholds)
        per_task.append({
            "key": task.key,
            "url": task.url,
            "status": task.status,
            "description": description_check(task, thresholds),
            "commit": commit_check(task, thresholds),
            "comment": comment_check(task, facts.date, thresholds),
            "done": {"passed": credit >= 1.0, "credit": round(credit, 3), "rule": rule},
        })

    def share(check: str) -> float:
        return (sum(1 for e in per_task if e[check]["passed"]) / n) if n else 0.0

    checkin_pts = weights.checkin if facts.checked_in else 0.0
    picked_pts = weights.picked if n else 0.0
    description_pts = weights.description * share("description")
    commit_pts = weights.commit * share("commit")
    comment_pts = weights.comment * share("comment")

    # Summed at full precision; only the outputs are rounded. Rounding the
    # components first makes them fail to add up to their own total.
    process = checkin_pts + picked_pts + description_pts + commit_pts + comment_pts

    done_count = sum(1 for e in per_task if e["done"]["passed"])
    credit_sum = sum(e["done"]["credit"] for e in per_task)
    vf = volume_factor(n, facts.median_picked, thresholds.min_median_tasks)
    delivery = weights.done * (credit_sum / n if n else 0.0) * vf

    flags = []
    if attendance == HALF_DAY:
        flags.append("half_day")
    if not n:
        flags.append("no_tasks")

    unplanned = any(is_incident(t, thresholds) for t in ordered)
    if unplanned:
        # No committed tickets to deliver against, so the day is scored on
        # process alone, rescaled to 100. Detected from the Jira issue type —
        # nobody decides it.
        flags.append("unplanned_work")
        delivery = 0.0
        total = process * (100.0 / weights.process_max) if weights.process_max else 0.0
    else:
        total = process + delivery
        if vf < 1.0:
            flags.append("under_committed")

    record.update({
        "tasks_done": done_count,
        "tasks_credit": round(credit_sum, 2),
        "points": {
            "checkin": _round(checkin_pts), "picked": _round(picked_pts),
            "description": _round(description_pts), "commit": _round(commit_pts),
            "comment": _round(comment_pts), "done": _round(delivery),
        },
        "process": _round(process),
        "delivery": _round(delivery),
        "volume_factor": round(vf, 3),
        "total": _round(total),
        "band": band_of(total),
        "flags": flags,
        "evidence": {
            "checkin": {"passed": bool(facts.checked_in)},
            "picked": {"passed": bool(n), "count": n},
            "tasks": per_task,
        },
    })
    return record


# --------------------------------------------------------------------------
# Sheet surfaces
# --------------------------------------------------------------------------
def _cell(value) -> str:
    return "" if value is None else str(value)


def daily_row(record: dict) -> list[str]:
    """One sheet row per developer-day, column-aligned with DAILY_HEADERS."""
    points = record.get("points") or {}
    return [
        record["date"],
        record["developer"],
        record.get("attendance", ""),
        record.get("status", ""),
        record.get("reason", ""),
        ", ".join(record.get("picked_tasks") or []),
        _cell(record.get("tasks_picked", 0)),
        _cell(record.get("tasks_done", 0)),
        _cell(record.get("tasks_credit", 0.0)),
        _cell(points.get("checkin")),
        _cell(points.get("picked")),
        _cell(points.get("description")),
        _cell(points.get("commit")),
        _cell(points.get("comment")),
        _cell(points.get("done")),
        _cell(record.get("process")),
        _cell(record.get("delivery")),
        _cell(record.get("volume_factor")),
        _cell(record.get("total")),
        record.get("band", ""),
        ", ".join(record.get("flags") or []),
        json.dumps(record.get("evidence") or {}, ensure_ascii=False, separators=(",", ":")),
        _cell(record.get("computed_at")).replace("T", " "),
    ]


def merge_daily(existing: list[list[str]], new_rows: list[list[str]]) -> list[list[str]]:
    """Upsert `new_rows` into `existing` by (date, developer), newest first.

    The tab accumulates across runs rather than being replaced; keying on the
    pair means the nightly recompute corrects a row instead of appending a
    second one for the same day.
    """
    width = max(DATE_COLUMN, DEVELOPER_COLUMN)
    rows = [list(r) for r in existing
            if r and len(r) > width and r[DATE_COLUMN] and r[DEVELOPER_COLUMN]]
    index = {(r[DATE_COLUMN], r[DEVELOPER_COLUMN]): i for i, r in enumerate(rows)}
    for row in new_rows:
        key = (row[DATE_COLUMN], row[DEVELOPER_COLUMN])
        if key in index:
            rows[index[key]] = row
        else:
            index[key] = len(rows)
            rows.append(row)
    rows.sort(key=lambda r: (r[DATE_COLUMN], r[DEVELOPER_COLUMN]), reverse=True)
    return rows


def _weight_of(record: dict) -> float:
    """How much a day counts toward an average: half days count half."""
    return 0.5 if HALF_DAY in _norm(record.get("attendance", "")) else 1.0


def period_average(records: list[dict]) -> float | None:
    """Weighted mean total. `Not Scored` excluded, half days at 0.5 weight.

    None when nothing in the period was scored — which is not the same as an
    average of zero, and must not be rendered as one.
    """
    scored = [r for r in records
              if r.get("status") == SCORED and r.get("total") is not None]
    if not scored:
        return None
    weight = sum(_weight_of(r) for r in scored)
    if weight <= 0:
        return None
    return round(sum(float(r["total"]) * _weight_of(r) for r in scored) / weight, 1)


def month_matrix(records: list[dict], year: int, month: int,
                 order: list[str]) -> tuple[list[str], list[list[str]]]:
    """Developer x day grid of totals for 'Scorecard Monthly' (SCORING.md §8.2).

    Mirrors the attendance month tabs: one column per calendar day, headed
    MM-DD-YY. `Not Scored` days are left blank rather than zero, so they are
    visibly absent from the row instead of dragging the average down.
    """
    last = (dt.date(year + month // 12, month % 12 + 1, 1) - dt.timedelta(days=1)).day
    days = [dt.date(year, month, d) for d in range(1, last + 1)]
    header = ["Developer"] + [d.strftime("%m-%d-%y") for d in days] + ["Average"]

    by_person: dict[str, dict[str, dict]] = {}
    for record in records:
        by_person.setdefault(record["developer"], {})[record["date"]] = record

    rows = []
    for name in order:
        owned = by_person.get(name, {})
        cells = []
        for day in days:
            record = owned.get(day.isoformat())
            total = record.get("total") if record else None
            cells.append("" if not record or record.get("status") != SCORED
                         or total is None else _cell(total))
        average = period_average(list(owned.values()))
        rows.append([name] + cells + ["" if average is None else str(average)])
    return header, rows


# --------------------------------------------------------------------------
# Slack surfaces
# --------------------------------------------------------------------------
def compose_slack_dm(record: dict) -> str:
    """The individual breakdown — the only place a person's score is stated."""
    date = record["date"]
    if record.get("status") != SCORED:
        return (f"*Scorecard — {date}*\nNot scored: {record.get('reason') or 'no data'}.\n"
                f"This day is excluded from your average and will be retried automatically.")

    points = record.get("points") or {}
    lines = [
        f"*Scorecard — {date}*  ·  *{record['total']}/100*  ({record.get('band', '')})",
        f"Process {record.get('process')}/40  ·  Delivery {record.get('delivery')}/60",
        "",
    ]
    if record.get("attendance", "").strip().lower() == "absent":
        lines.append("Marked absent — no checks were run.")
        return "\n".join(lines).rstrip()

    for label, key, cap in (("Check-in", "checkin", 10), ("Task picked", "picked", 5),
                            ("Jira description", "description", 10), ("Commit linked", "commit", 5),
                            ("Jira comment", "comment", 10), ("Tasks done", "done", 60)):
        lines.append(f"  • {label}: {points.get(key, 0)}/{cap}")

    tasks = (record.get("evidence") or {}).get("tasks") or []
    if tasks:
        lines += ["", f"Tasks ({record.get('tasks_done', 0)}/{record.get('tasks_picked', 0)} done, "
                      f"{record.get('tasks_credit', 0)} credit)"]
        for task in tasks:
            passed = [name for name in ("description", "commit", "comment")
                      if (task.get(name) or {}).get("passed")]
            done = task.get("done") or {}
            credit = done.get("credit", 0)
            head = f"<{task['url']}|{task['key']}>" if task.get("url") else task["key"]
            lines.append(f"  • {head} — {task.get('status', '')} · delivery {credit}"
                         f"{' · ' + ', '.join(passed) if passed else ' · nothing else recorded'}")
    if record.get("flags"):
        lines += ["", f"_Flags: {', '.join(record['flags'])}_"]
    return "\n".join(lines).rstrip()


PUBLIC_ORDERS = ("roster", "score")


def _state_of(record: dict) -> str:
    """The one-word answer to 'what happened to this person today'."""
    if record.get("status") != SCORED:
        reason = record.get("reason") or "no data"
        return f"Not scored — {reason}"
    if _norm(record.get("attendance", "")) == "absent":
        return "Absent"
    band = record.get("band", "")
    return f"{band} (half day)" if HALF_DAY in _norm(record.get("attendance", "")) else band


def compose_slack_roster(records: list[dict], *, order: str = "roster") -> str:
    """Every developer's score for the day, as one channel post.

    A fixed-width block, because Slack renders it monospaced and the columns
    only line up that way — a bulleted list of six numbers per person is
    unreadable at a glance.

    `order` is "roster" (the order developers appear in the roll-call) or
    "score" (highest first). Roster order is the default: it publishes exactly
    the same information without turning the post into a ranking, which is a
    presentation choice rather than a data one — see SCORING.md §8.3.
    """
    if not records:
        return "*Daily Scorecard* · nothing to report."

    rows = list(records)
    if order == "score":
        rows.sort(key=lambda r: (r.get("total") is None, -(r.get("total") or 0)))

    date = records[0].get("date", "")
    scored = [r for r in records if r.get("status") == SCORED]
    average = period_average(records)
    absent = sum(1 for r in scored if _norm(r.get("attendance", "")) == "absent")
    unscored = len(records) - len(scored)

    counts = [f"{len(scored)} scored"]
    if absent:
        counts.append(f"{absent} absent")
    if unscored:
        counts.append(f"{unscored} not scored")

    width = max([len(r.get("developer", "")) for r in rows] + [9])
    head = f"{'Developer':<{width}}  {'Total':>5}  {'Process':>7}  {'Delivery':>8}  State"
    lines = [head]
    for record in rows:
        name = str(record.get("developer", ""))[:width]
        if record.get("status") != SCORED:
            lines.append(f"{name:<{width}}  {'—':>5}  {'—':>7}  {'—':>8}  {_state_of(record)}")
            continue
        lines.append(
            f"{name:<{width}}  {record.get('total', 0):>5}  {record.get('process', 0):>7}"
            f"  {record.get('delivery', 0):>8}  {_state_of(record)}"
        )

    return (
        f"*Daily Scorecard — {date}*\n"
        f"Team average *{'—' if average is None else average}*/100 · {' · '.join(counts)}\n"
        "```\n" + "\n".join(lines) + "\n```\n"
        "_Process 40 = check-in 10 · task id 5 · description 10 · commit 5 · comment 10._\n"
        "_Delivery 60 = per task: Done 100% · in review 50% (100% with a commit id) "
        "· in progress 25%._"
    )


def compose_nudge(mention: str, channel_id: str) -> str:
    """The reminder posted when a developer has named no ticket by capture time.

    Posted in the check-in channel with the person tagged, rather than DM'd:
    Slack files bot DMs under "Apps" where they are easily missed, and the
    reminder only works if it is seen while the day can still be fixed.

    `mention` is the raw Slack mention (`<@U123>`) so the person is notified.
    """
    return (
        f"{mention} You have not named a Jira ticket in <#{channel_id}> today, "
        f"so there is nothing to link your work to.\n\n"
        f"Post your stand-up with the ticket id in it — for example `HIR-98` or "
        f"`WS-256`. Any of these forms are read: `HIR-98`, `HIR 98`, `hir-98`.\n\n"
        f"Until then today counts as no task picked, which caps the day at "
        f"10/100 however much work you do. It is picked up automatically once "
        f"you post — nobody has to be told."
    )


def compose_slack_team(records: list[dict]) -> str:
    """The team aggregate only — used when per-person scores are not published."""
    if not records:
        return "*Scorecard* · nothing to report."
    date = records[0].get("date", "")
    scored = [r for r in records if r.get("status") == SCORED]
    average = period_average(records)
    absent = sum(1 for r in scored if _norm(r.get("attendance", "")) == "absent")
    unscored = [r for r in records if r.get("status") != SCORED]

    lines = [f"*Scorecard — {date}*", ""]
    lines.append(f"  • Team average: {'—' if average is None else average}/100"
                 f"  (from {len(scored)} scored)")
    if scored:
        lines.append(f"  • Process: {round(sum(r['process'] for r in scored) / len(scored), 1)}/40"
                     f"  ·  Delivery: {round(sum(r['delivery'] for r in scored) / len(scored), 1)}/60")
    lines.append(f"  • Absent: {absent}")
    if unscored:
        lines.append(f"  • Not scored: {len(unscored)} "
                     f"({', '.join(sorted({r.get('reason', '') for r in unscored if r.get('reason')}))})")
    lines += ["", "_Individual breakdowns have been sent by DM._"]
    return "\n".join(lines)
