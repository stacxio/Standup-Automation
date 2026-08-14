# `scorecard.py` — Module Contract

The scoring rules from [SCORING.md](SCORING.md), as a pure module.

`src/standup_summarizer/scorecard.py` performs **no I/O**: no Slack, Jira, Sheets
or clock calls. It receives facts and returns a record. `build_scorecard.py`
supplies the I/O, exactly as `archive_meetings.py` does for `archive.py`.

Two consequences worth stating, because they are the reason for the split:

* **Every rule is unit-testable** against literal fixtures — no network, no mocks.
* **The scorer is a pure function of its inputs.** Re-running it on the same facts
  produces byte-identical output, which is what makes a score defensible when
  nobody reviews it (SCORING.md §7.2 recompute relies on this).

`date` and `computed_at` are **parameters, never read from the clock** — the same
convention `archive.build_meta` uses for `archived_at`.

---

## 1. Input shapes

```python
@dataclass(frozen=True)
class CommentFacts:
    """One Jira comment, as fetched. Rules are applied by this module."""
    body: str
    created: dt.date
    authored_by_developer: bool     # resolved by the caller via roster name matching
    media: tuple[str, ...] = ()     # attached image filenames; a screenshot-only
                                    # comment has an empty body but is a real update


@dataclass(frozen=True)
class TaskFacts:
    """One picked Jira issue at the cutoff."""
    key: str                        # canonical, e.g. "SP-12"
    description: str                # plain text; caller has run adf_to_text
    parent_description: str = ""    # for a sub-task that carries none of its own
    status: str = ""                # raw, e.g. "Review"
    status_category: str = ""       # "To Do" | "In Progress" | "Done"
    issue_type: str = ""            # e.g. "Story", "Bug", "Incident"
    has_linked_commit: bool = False # Jira dev panel: branch/commit/PR present
    comments: tuple[CommentFacts, ...] = ()
    url: str = ""                   # for the evidence column


@dataclass(frozen=True)
class DayFacts:
    """Everything needed to score one developer for one day."""
    developer: str                  # Master roster short name
    date: dt.date
    attendance: str                 # Present | Half Day | Absent | Leave | Weekend | Holiday
    checked_in: bool                # roll-call in the check-in channel before cutoff
    picked_tasks: tuple[str, ...]   # frozen at capture (SCORING.md §3)
    tasks: tuple[TaskFacts, ...]    # facts for the picked keys
    median_picked: float | None = None   # trailing-30-day median; None = insufficient history
    data_ok: bool = True            # False -> Not Scored
    data_error: str = ""            # why, e.g. "jira: 503"
```

**Name resolution stays with the caller.** `authored_by_developer` is a loose
roster match (the same one `verify_standup.py` uses for assignee→developer). That
is an identity concern, not a scoring rule, and keeping it out means this module
never needs the roster.

**Everything else stays here.** Comment length, recency, duplication and commit-
reference detection are *rules*, so they operate on `body` inside this module and
are covered by tests.

### 1.1 Configuration

```python
@dataclass(frozen=True)
class Weights:
    checkin: float = 10.0
    picked: float = 5.0
    description: float = 10.0
    commit: float = 5.0
    comment: float = 10.0
    done: float = 60.0

    def validate(self) -> None:
        """Raise ValueError unless the six weights sum to 100."""


@dataclass(frozen=True)
class Thresholds:
    min_description_chars: int = 30
    min_comment_chars: int = 20
    review_statuses: frozenset[str] = frozenset({"In Review", "Code Review", "Review"})
    incident_types: frozenset[str] = frozenset({"Incident", "Support"})
    incident_projects: frozenset[str] = frozenset()
    min_median_tasks: float = 2.0

    # Adjustments for how this team uses Jira — see SCORING.md §4.
    parent_description_fallback: bool = True   # sub-task inherits parent's description
    media_counts_as_comment: bool = True       # screenshot-only comment is an update
    commit_in_comment_counts: bool = True      # commit id in a comment (no SCM linked)

    # Delivery credit per task state.
    credit_done: float = 1.0
    credit_review_with_commit: float = 1.0
    credit_review: float = 0.5
    credit_in_progress: float = 0.25
    credit_todo: float = 0.0
```

Both are built in `config.py` from the `SCORE_*` env keys (SCORING.md §10).
`validate()` is called once at start-up so a misconfigured deployment fails
immediately rather than silently scoring out of 95.

Status and type comparisons are **case-folded and whitespace-stripped** on both
sides — Jira workflow names are edited by hand and `"In review"` must not fail to
match `"In Review"`.

---

## 2. Public API

```python
def score_day(facts: DayFacts, weights: Weights, thresholds: Thresholds,
              *, computed_at: dt.datetime) -> dict:
    """The scoring rule. One developer, one day -> one record (§3)."""
```

Everything else is a helper that `score_day` composes, exported so each rule can
be tested in isolation:

```python
def has_commit_reference(text: str) -> bool:
    """§4.6 — a git commit URL, or a standalone 7-40 char hex token that is
    neither a Jira key nor part of a longer word."""

def description_check(task: TaskFacts, thresholds: Thresholds) -> dict:
    """§4.3 — own description, else the parent's. Reports which source passed."""

def commit_check(task: TaskFacts, thresholds: Thresholds) -> dict:
    """§4.4 — dev panel (authoritative), else a commit id named in a comment."""

def comment_check(task: TaskFacts, day: dt.date, thresholds: Thresholds) -> dict:
    """§4.5 — author and date always; then text length + non-duplicate, OR
    attached media."""

def task_credit(task: TaskFacts, thresholds: Thresholds) -> tuple[float, str]:
    """§4.6 -> (credit 0.0-1.0, rule) where rule is
    "done" | "review_with_commit" | "review" | "in_progress" | "todo"."""

def task_is_done(task: TaskFacts, thresholds: Thresholds) -> tuple[bool, str]:
    """Whether a task reached full credit. A thin wrapper over task_credit."""

def volume_factor(n: int, median: float | None, min_median: float) -> float:
    """§5.1 — 1.0 unless the developer under-committed against their own median."""

def band_of(total: float) -> str:
    """§6 — Excellent | On Track | Needs Attention | At Risk."""
```

### 2.1 Sheet and Slack surfaces

Mirrors `archive.py`'s `INDEX_HEADERS` / `index_row` / `merge_index` /
`compose_slack`:

```python
DAILY_HEADERS: list[str]        # the 'Scorecard Daily' tab, SCORING.md §8.1
DATE_COLUMN: int
DEVELOPER_COLUMN: int

def daily_row(record: dict) -> list[str]:
    """One sheet row, column-aligned with DAILY_HEADERS."""

def merge_daily(existing: list[list[str]], new_rows: list[list[str]]) -> list[list[str]]:
    """Upsert by (date, developer), newest first. The tab accumulates across
    runs; keying on the pair means the nightly recompute corrects a row instead
    of appending a duplicate."""

def month_matrix(records: list[dict], year: int, month: int,
                 order: list[str]) -> tuple[list[str], list[list[str]]]:
    """Developer x day grid of totals for 'Scorecard Monthly' (§8.2)."""

def period_average(records: list[dict]) -> float | None:
    """Weighted mean: Not Scored excluded, Half Day at 0.5 weight.
    None when nothing in the period was scored."""

def compose_slack_dm(record: dict) -> str:
    """The individual breakdown."""

def compose_slack_team(records: list[dict]) -> str:
    """The team aggregate. No per-person ranking (SCORING.md §14)."""
```

---

## 3. Output shape

`score_day` returns a plain dict — serialisable straight to the sheet and to the
`evidence` JSON column, the same choice `archive.build_meta` makes.

```python
{
  "developer": "Raghul",
  "date": "2026-08-12",
  "computed_at": "2026-08-12T18:00:04",
  "schema": 1,

  "attendance": "Present",
  "status": "Scored",              # "Scored" | "Not Scored"
  "reason": "",                    # populated only when Not Scored

  "picked_tasks": ["SP-12", "SP-15", "HIR-72"],
  "tasks_picked": 3,
  "tasks_done": 2,                 # at full credit
  "tasks_credit": 2.25,            # summed §4.6 credit — partial progress, auditable

  "points": {                      # rounded to 1dp for display only
    "checkin": 10.0, "picked": 5.0, "description": 6.7,
    "commit": 3.3,  "comment": 6.7, "done": 45.0,
  },
  "process": 31.7,                 # 0-40
  "delivery": 45.0,                # 0-60
  "volume_factor": 1.0,
  "total": 76.7,                   # 0-100
  "band": "On Track",
  "flags": [],                     # half_day | no_tasks | unplanned_work | under_committed

  "evidence": {
    "checkin": {"passed": True},
    "picked": {"passed": True, "count": 3},
    "tasks": [
      {
        "key": "SP-12",
        "url": "https://.../browse/SP-12",
        "status": "Done",
        "description": {"passed": True,  "chars": 412, "source": "issue"},
        "commit":      {"passed": True,  "source": "dev_panel"},
        "comment":     {"passed": True,  "chars": 88, "source": "text",
                        "created": "2026-08-12"},
        "done":        {"passed": True,  "credit": 1.0, "rule": "done"},
      },
      # ... one per picked task, in picked order
    ],
  },
}
```

Every task appears in `evidence.tasks` whether it passed or failed, in the order
it was picked. A failed check carries **why** — `{"passed": false, "chars": 8}`
says the description was too short, not merely that something was wrong. This
column is what SCORING.md §7.2 relies on in place of an appeals process.

### 3.1 Rounding

Components are summed at **full precision**; only the final values are rounded to
one decimal. The worked example shows why: the rounded components `6.7 + 1.7 +
6.7` sum to `15.1`, but the true sum is `15.0`, giving `process = 30.0` not
`30.1`. Rounding first would make the row's own columns fail to add up.

---

## 4. Decision order

`score_day` resolves in this sequence. The first match wins.

| # | Condition | Result |
|---|-----------|--------|
| 1 | `not data_ok` | `Not Scored`, `reason = data_error` |
| 2 | a key in `picked_tasks` has no matching `TaskFacts` | `Not Scored`, `reason = "incomplete jira facts: SP-15"` |
| 3 | `attendance` in Leave / Weekend / Holiday | `Not Scored`, `reason = attendance` |
| 4 | `attendance == "Absent"` | `Scored`, `total = 0`, all points 0 |
| 5 | any picked task is incident-typed or in an incident project | score process only, `total = process × 2.5`, flag `unplanned_work` |
| 6 | otherwise | the full rubric, §5 |

Rule 2 matters. If Jira returned facts for two of three picked tasks, scoring
what came back **understates** the developer — the missing task can only ever
lose them points. Partial data is a data failure, not a low score, so the day
goes `Not Scored` and the 02:00 pass retries it.

Rule 4 is the one place a genuine `0` is written. Everywhere a fact is missing
rather than bad, the answer is `Not Scored` (SCORING.md §7).

---

## 5. Canonical test case

Asserts the SCORING.md §11.1 worked example end to end.

```python
facts = DayFacts(
    developer="Raghul",
    date=dt.date(2026, 8, 12),
    attendance="Present",
    checked_in=True,
    picked_tasks=("SP-12", "SP-15", "HIR-72"),
    median_picked=3.0,
    tasks=(
        TaskFacts("SP-12", "x" * 412, "Done", "Done", "Story",
                  has_linked_commit=True,
                  comments=(CommentFacts(
                      "Implemented the endpoint and merged a1b2c3d into main.", TODAY, True),)),
        TaskFacts("SP-15", "x" * 200, "In Review", "In Progress", "Task",
                  has_linked_commit=False,
                  comments=(CommentFacts(
                      "PR is up: https://github.com/o/r/commit/9f8e7d6", TODAY, True),)),
        TaskFacts("HIR-72", "fix", "In Progress", "In Progress", "Bug",
                  has_linked_commit=False, comments=()),
    ),
)

record = score_day(facts, Weights(), Thresholds(), computed_at=NOON)

assert record["points"] == {"checkin": 10.0, "picked": 5.0, "description": 6.7,
                            "commit": 3.3, "comment": 6.7, "done": 45.0}
assert record["process"] == 31.7
assert record["delivery"] == 45.0       # (1.0 + 1.0 + 0.25) / 3 x 60
assert record["total"] == 76.7
assert record["band"] == "On Track"
assert record["tasks_credit"] == 2.25
```

`SP-15` exercises the `review_with_commit` rule, and its commit URL also earns
the §4.4 commit point through the comment fallback; `HIR-72` fails description
(3 chars), commit and comment, but still earns 0.25 delivery credit for being in
progress; `SP-12` passes everything. Both comments are deliberately over
`min_comment_chars` — a shorter one would fail §4.5 and change the expected
`comment` points.

Rounding is exercised separately (§3.1) by a fixture that produces 2/3, 1/3, 2/3
— this case's components happen to round cleanly.

---

## 6. Test matrix

One test per rule, plus the boundaries that decide fairness.

| Area | Cases |
|------|-------|
| `Weights.validate` | sums to 100 passes; 95 and 105 raise |
| Decision order | each of the six rows in §4, and that rule 2 beats rule 4 |
| Check-in | present + roll-call; present, no roll-call; roll-call after cutoff |
| Task picked | one key; several; none (`n = 0` → max 10, flag `no_tasks`) |
| Description | at, one under, one over `min_description_chars`; empty; whitespace-only; sub-task inherits parent; own description wins over parent; thin parent does not rescue; fallback disabled |
| Commit | dev panel true / false; commit id in a comment passes and reports source `comment`; comment without a commit id fails; fallback disabled |
| Comment | passes; wrong author; yesterday's date; too short; duplicate of the previous comment; duplicate differing only in whitespace or case; screenshot-only comment passes with source `media`; someone else's screenshot fails; media rule disabled |
| `has_commit_reference` | full git URL; bare 7-char sha; 40-char sha; `deadbeef`; `facade`; a Jira key `SP-1234567`; hex inside a longer word; digits only |
| `task_credit` | each rung of the ladder — Done, review+commit, review, in progress, To Do; a commit id must not promote a To Do or In Progress task; the ladder is configurable |
| `volume_factor` | `n ≥ median`; `n < median`; `median is None`; median below `min_median_tasks`; `n = 0` |
| Averaging | `Not Scored` excluded; Half Day at 0.5 weight; all-`Not Scored` period → `None` |
| Rounding | the §5 case, asserting `30.0` not `30.1` |
| Determinism | `score_day` twice on the same facts → identical dicts |
| `merge_daily` | insert; upsert same (date, developer); ordering newest-first |

The `has_commit_reference` false-positive row is the one most worth writing
first — `deadbeef` and `facade` are real English-looking tokens that a naive
`[0-9a-f]{7,40}` matches, and a false positive there hands out both the 5-point
commit check and, on an In Review ticket, the full 60.

---

## 7. Build order

1. `Weights`, `Thresholds`, `validate()` — nothing else can be tested until the
   config shape is fixed.
2. `has_commit_reference` + its false-positive tests.
3. `comment_passes`, `task_is_done`, `volume_factor`, `band_of` — independent,
   testable in any order.
4. `score_day` composing them, with the §4 decision order.
5. The §5 canonical case as an end-to-end assertion.
6. `DAILY_HEADERS` / `daily_row` / `merge_daily` / `month_matrix`.
7. `compose_slack_dm` / `compose_slack_team`.

Only after all seven does `build_scorecard.py` get written. It should contain no
arithmetic at all — fetch, call `score_day`, write.
