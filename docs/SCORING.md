# Daily Performance Scoring — Specification

Every developer receives one score per working day, from 0 to 100. The score is
computed automatically from Slack and Jira, on a schedule, with **no manual
entry, no manual review, and no manual override**.

Two properties this spec is built to guarantee:

* **Reproducible** — the same inputs always produce the same score. The reasoning
  engine never produces a number; it may only write commentary. This follows the
  AI caveat in [PROCESS.md](PROCESS.md) §5.
* **Explainable** — every score carries the evidence that produced it (which
  check passed, which issue, which timestamp), so any score can be traced to a
  Slack message or a Jira field without anyone's opinion being involved.

---

## 1. The rubric

| # | Check | Weight | Scope |
|---|-------|--------|-------|
| 1 | Check-in posted in the check-in channel | **10** | per day |
| 2 | Task ID mentioned (task picked) | **5** | per day |
| 3 | Jira description present on the task | **10** | per task |
| 4 | Commit ID linked to the task | **5** | per task |
| 5 | Jira comment on the task by the developer, on a task assigned to them | **10** | per task |
| 6 | Task Done — or In Review with a commit ID in a comment | **60** | per task |
| | **Total** | **100** | |

Checks 1–5 (40 points) measure **process compliance**. Check 6 (60 points)
measures **delivery**. The dashboard reports the two separately as well as
combined; see §8.

---

## 2. Attendance gate

Attendance is read from the roll-call parse in `build_attendance.py` and is
evaluated before any other check.

| Attendance | Result |
|------------|--------|
| **Absent** | **0** — a real score of zero. No further checks run. |
| **Leave** | **Not Scored** — excluded from all averages. |
| **Weekend / Holiday** | **Not Scored** — excluded from all averages. |
| **Half Day** | Scored normally; counts as a **0.5-weight day** in period averages. |
| **Present** | Scored normally, full weight. |

Approved leave is excluded rather than scored 0. Scoring it as 0 would make
approved leave indistinguishable from non-performance and would discourage
people from taking it.

`Not Scored` and `0` are **different values** and must never be collapsed. See
§7.

---

## 3. Definitions

**Scoring date** — the calendar date being scored, in `TIMEZONE`.

**Picked tasks** — the set of Jira issue keys extracted from the developer's
stand-up message on the scoring date, taken at the capture time
(`SCORE_CAPTURE_HOUR`, default 11:00).

A set with tickets in it is **frozen**: it is not recomputed later, so a task
cannot be silently dropped from a commitment during the day, and one added at
17:00 is not counted either.

An **empty** set stays open. There is no commitment to drop, and the developer is
DM'd at capture time (§3.1) telling them to post a ticket id — holding them to a
blank while asking them to fix it would contradict the reminder. Each later
capture re-reads only the empty entries; the moment one names a ticket it freezes
like the rest.

Keys are extracted with the same tolerant parser `build_report.py` uses, which
accepts hand-typed forms (`HIR - 72`, `BHA- 79`, `SP12`).

### 3.1 The capture-time reminder

A developer with no ticket id at capture time is DM'd while the day can still be
fixed — by the cutoff the set is read for scoring and nothing more can be added.

Nobody is chased at the weekend, on approved leave, or when the roll-call records
them **Absent**, which is an answer rather than a silence. An *unknown* attendance
is still chased: at 11:00 the roll-call is often not posted yet, and the missing
ticket id matters either way.

The reminder is sent once per developer per day — only those seen for the first
time that day — so re-reading an empty entry never repeats it. A developer with
no resolvable Slack id is reported to `SCORE_OPS_CHANNEL_ID` instead of the
reminder disappearing.

Set `SCORE_NUDGE_ENABLED=false` to turn it off.

**Cutoff** — `SCORE_CUTOFF_HOUR`, default 18:00. All per-task checks are
evaluated against Jira state at the cutoff.

**Their comment** — a Jira comment whose author resolves to the developer via
the same loose name matching `verify.py` uses for assignee→roster.

---

## 4. Detection rules

Every check is deterministic and reads an existing function.

### 4.1 Check-in — 10 points, per day, binary

A roll-call entry for the developer exists in `SLACK_CHANNEL_ID` on the scoring
date, posted before the cutoff, with status `Present` or `Half Day`.

> Source: `_rollcall_entry` / `_attendance_of` in `build_attendance.py`.

### 4.2 Task picked — 5 points, per day, binary

The developer's stand-up message on the scoring date contains **at least one**
valid Jira issue key. A key is valid if its prefix is in `SCORE_PROJECT_PREFIXES`
(blank derives them from `SUMMARY_CHANNEL_ROUTES` + `ARCHIVE_PROJECT_NAMES`);
keys for unknown projects do not count.

> Source: the issue-key parser used by `build_report.py`, then the prefix filter.

**The filter is load-bearing.** Stand-ups are prose and the parser matches
anything shaped like `ABC-123`. Real stand-ups yielded `ST-11`, `PI-01`,
`ORG-404`, `TEMP-2` and `KSK-0003` — none of them Jira issues. Since a key Jira
cannot resolve sends the whole developer-day to `Not Scored` (§4 rule 2), one
such phrase silently costs that person their score. Adding the filter recovered
7 of 98 developer-days in a 14-day backfill.

### 4.3 Jira description — 10 points, per task

The issue description, rendered to plain text, is at least
`SCORE_MIN_DESCRIPTION_CHARS` characters (default 30). A **sub-task with no
description of its own inherits its parent's** (`SCORE_PARENT_DESCRIPTION`).

The **author of the description is irrelevant** — product managers normally write
descriptions. The check asks whether the ticket is documented, not who
documented it.

> Source: `jira.issue_detail` → `jira.adf_to_text`; the parent via
> `detail["parent_key"]`.

**Why the length floor:** without it, a description of `"fix"` scores the full 10
points. This is the cheapest check in the rubric to satisfy trivially.

**Why the parent fallback:** this team breaks work into sub-tasks and documents
the parent. Measured against the sub-task alone the check was unreachable for
whole sprints — it was measuring the issue hierarchy, not whether anyone can tell
what the work is.

### 4.4 Commit ID — 5 points, per task

Either:

* a commit, branch, or pull request is linked in the Jira **development panel**
  (source `dev_panel`); or
* a comment on the issue **names a commit** (source `comment`,
  `SCORE_COMMIT_IN_COMMENT`).

> Source: `jira.issue_dev_info`, then `jira.issue_comments`.

The development panel is authoritative — it cannot be satisfied by typing — and
is always reported as the source when it has data. The comment fallback exists
because **this Jira has no SCM integration**: the dev-status API answers cleanly
but every count is zero and `byInstanceType` is empty, so the panel is
permanently blank and the check was impossible for everyone. Connecting GitHub to
Jira would make the panel the real source; until then the fallback is what makes
this 5 points a measurement rather than a constant.

A commit reference in text means one of:

* a git hosting URL containing a commit path (e.g. `.../commit/<sha>`), or
* a standalone 7–40 character hex token containing both a digit and a letter
  a–f, that is not a Jira issue key and not part of a longer word.

### 4.5 Jira comment — 10 points, per task

The issue is **assigned to the developer** (`SCORE_REQUIRE_ASSIGNEE`, default
on), and carries a comment **authored by the developer** and **created on the
scoring date**, and then either:

| Kind | Condition |
|------|-----------|
| Text | At least `SCORE_MIN_COMMENT_CHARS` characters (default 20), and not a duplicate — normalised hash differs from that developer's previous comment on the issue. |
| Screenshot | Carries attached media (`SCORE_MEDIA_IS_COMMENT`). |

> Source: `jira.issue_comments`; media via `jira.adf_media_names`.

The author, date and duplicate conditions are what stop `"working on it"` pasted
into every picked ticket each morning from being a guaranteed 10 points a day.

**Why the assignee gate:** these 10 points are evidence of a developer moving
their *own* ticket forward. A comment on a colleague's issue is collaboration —
worth doing, but it is not that, and without the gate anyone could name an
active ticket they do not own and comment their way to the points. An
**unassigned** issue fails the gate too: the rule is "assigned to you", and
nobody is. The assignee's Jira display name is folded onto the roster short by
the same loose matching used for comment authors, so "Malleshwaran M", "Gokul N"
and a lowercase "sahil" all resolve.

The gate costs **only these 10 points**. The task still earns description,
commit and its full 60 delivery credit — working someone else's ticket is not
treated as having done nothing.

**Why media counts:** a screenshot-only comment is an ADF `media` node and
flattens to zero characters, so it read as "said nothing". Posting evidence of
progress is exactly the behaviour this check exists to reward.

### 4.6 Delivery credit — 60 points, per task

Each task earns credit by its state at the cutoff:

| State | Credit | Config |
|-------|--------|--------|
| Status category **Done** | 100% | `SCORE_CREDIT_DONE` |
| **In review** with a commit reference in a comment | 100% | `SCORE_CREDIT_REVIEW_COMMIT` |
| **In review** without one | **50%** | `SCORE_CREDIT_REVIEW` |
| **In progress** | **25%** | `SCORE_CREDIT_IN_PROGRESS` |
| To Do / untouched | 0% | `SCORE_CREDIT_TODO` |

"In review" means the status is in `SCORE_REVIEW_STATUSES` (default
`In Review,Code Review,Review` — this Jira uses plain `Review`).

> Source: `jira.issue_status` for status; `jira.issue_comments` for the
> commit reference.

**Why a ladder rather than done/not-done:** at a 6pm cutoff most in-flight work
sits in review. Binary scoring paid the same nothing for a ticket with the work
finished and awaiting review as for one never started, which is wrong in both
directions. Every rung is configurable, because the right ladder depends on how a
team uses its workflow.

---

## 5. Formulas

Let `P` be the set of picked tasks for the day, `n = |P|`.

```
checkin       = 10  if §4.1 passes else 0
picked        =  5  if §4.2 passes else 0

description   = 10 × (tasks in P passing §4.3) / n
commit        =  5 × (tasks in P passing §4.4) / n
comment       = 10 × (tasks in P passing §4.5) / n

process       = checkin + picked + description + commit + comment      # 0–40

credit        = sum of each task's §4.6 credit, 0.0–1.0 per task
delivery      = 60 × (credit / n) × volume_factor                      # 0–60

total         = round(process + delivery, 1)                           # 0–100
```

Per-task checks are **averaged across picked tasks**, which is what produces
partial credit: two of three tasks documented scores 6.7 of 10, not 0 and not 10.

If `n = 0` — present and checked in but no task mentioned — all per-task terms
are 0 and the maximum achievable score is 10. This is the correct outcome and
needs no special case.

### 5.1 Volume factor

```
median_n       = median picked-task count for this developer over the
                 trailing 30 scored days (0 if fewer than 10 scored days)
volume_factor  = min(1.0, n / median_n)   if median_n >= SCORE_MIN_MEDIAN_TASKS
                 1.0                       otherwise
```

The credit ratio alone rewards picking one easy task and finishing it — that
scores the same 60 points as finishing five. The volume factor scales delivery
down when someone commits to materially less than their own established baseline.

It compares a developer **to their own history, never to teammates**. There is no
cross-person target, so it needs no manager input and creates no ranking
dynamic. It can only reduce a score, never inflate one, and it stays inactive
until a developer has 10 scored days of history.

---

## 6. Bands

| Total | Band |
|-------|------|
| ≥ 85 | Excellent |
| 70–84 | On Track |
| 50–69 | Needs Attention |
| < 50 | At Risk |

Bands are presentation only. Nothing in the pipeline behaves differently based on
the band.

---

## 7. `Not Scored` is not zero

With no human watching the pipeline, **a broken integration must never appear as
poor performance.**

| Situation | Result |
|-----------|--------|
| Jira unreachable or rate-limited | `Not Scored`, retried by the recompute pass |
| Slack fetch failed | `Not Scored`, alert posted to the ops channel |
| Developer not on the Master roster | Skipped, alert posted to the ops channel |
| Developer absent | `0` |
| Developer present, posted no check-in | `0` on check-in — a real score |
| Developer present, checked in, picked no task | `10` — a real score |

`Not Scored` rows are excluded from every average and trend. Collapsing
`Not Scored` into `0` is the single most likely way this system produces an
unfair number, because it is silent.

### 7.1 Unplanned work

A developer who spends the day on a production incident has no picked tasks and
would otherwise score 10.

If the developer's stand-up references an issue whose type is in
`SCORE_INCIDENT_TYPES` (default `Incident,Support`), or whose project prefix is
in `SCORE_INCIDENT_PROJECTS`, the day is scored on **process compliance only,
rescaled to 100** (`total = process × 2.5`), and the row is flagged
`unplanned_work`.

This is detected from Jira issue type. Nobody decides it.

### 7.2 Self-correction instead of appeals

There is no manual override column and no appeals process. Instead, a recompute
pass re-runs the identical scoring function over the trailing
`SCORE_RECOMPUTE_DAYS` days (default 3) each night. A Jira update made late in
the evening self-corrects overnight without anyone requesting it.

Recompute is not a second opinion — it is the same deterministic function on
fresher data.

---

## 8. Output

### 8.1 `Scorecard Daily` tab — append-only fact table

One row per (date, developer). This tab is the source of truth for the dashboard
and is **never rewritten**, only appended to and upserted by key.

| Column | Notes |
|--------|-------|
| `date` | ISO date |
| `developer` | Master roster short name |
| `attendance` | Present / Half Day / Absent / Leave / Weekend |
| `status` | `Scored` / `Not Scored` |
| `picked_tasks` | Comma-separated issue keys, frozen at capture |
| `tasks_picked` | `n` |
| `tasks_done` | Count at full credit (§4.6) |
| `tasks_credit` | Summed §4.6 credit, so partial progress is auditable |
| `pts_checkin` … `pts_done` | One column per check, 6 columns |
| `process` | 0–40 |
| `delivery` | 0–60 |
| `volume_factor` | 0–1 |
| `total` | 0–100 |
| `band` | §6 |
| `flags` | `unplanned_work`, `half_day`, `no_tasks`, … |
| `evidence` | JSON: per-task pass/fail per check, with Jira links |
| `computed_at` | Timestamp of the run that wrote the row |

The `evidence` column is what replaces human review. A disputed score is resolved
by reading the row.

### 8.2 `Scorecard Monthly` tab

Developer × day matrix of totals, colour-coded, with a weighted period average
per developer — the same shape and colouring convention as the attendance month
tabs. `Not Scored` cells are blank and excluded from the average; Half Day cells
carry 0.5 weight.

### 8.3 Slack

**Every developer's score is posted to the check-in channel each day**
(`SCORE_PUBLIC_SCORES=true`, the default). One message, a fixed-width table, with
each person's total, process, delivery and state:

```
Developer  Total  Process  Delivery  State
Soma       100.0     40.0      60.0  Excellent
Raghul      76.7     31.7      45.0  On Track
Priya       70.0     40.0      30.0  On Track (half day)
Arjun        0.0      0.0       0.0  Absent
Meera          —        —         —  Not scored — jira: 503
```

The post always carries the rubric in its footer, so a number in the channel is
never separated from what produced it.

`SCORE_PUBLIC_ORDER` controls the order:

| Value | Effect |
|-------|--------|
| `roster` (default) | Roll-call order. Publishes every score without the post reading as a ranking. |
| `score` | Highest first. A visible leaderboard. |

Setting `SCORE_PUBLIC_SCORES=false` reverts to the aggregate-only post, with
individual breakdowns sent by DM (`SCORE_DM_ENABLED`). DMs can also run alongside
the public post, since the DM carries the per-task evidence the table has no room
for.

`Not Scored` appears as dashes with its reason, never as a zero — the same
distinction the scorer makes, preserved in the most public place it appears.

---

## 9. Schedule

| Time | Job | Action |
|------|-----|--------|
| `SCORE_CAPTURE_HOUR` (11:00) | `build_scorecard.py --capture` | Freeze picked tasks for the day; DM anyone who has named none yet (§3.1) |
| `SCORE_CUTOFF_HOUR` (18:00) | `build_scorecard.py --score` | Score against the frozen set, write tabs, DM |
| 02:00 | `build_scorecard.py --recompute --days 3` | Self-correcting pass (§7.2) |

The scoring job runs after `build_report.py` in `run_daily.py`, since it consumes
the same picked/completed task derivation.

---

## 10. Configuration

All weights and thresholds are configuration, not constants in code. They will be
tuned after the backfill in §12.

```
SCORE_WEIGHT_CHECKIN=10
SCORE_WEIGHT_PICKED=5
SCORE_WEIGHT_DESCRIPTION=10
SCORE_WEIGHT_COMMIT=5
SCORE_WEIGHT_COMMENT=10
SCORE_WEIGHT_DONE=60

SCORE_CAPTURE_HOUR=11
SCORE_CUTOFF_HOUR=18
SCORE_RECOMPUTE_DAYS=3

SCORE_MIN_DESCRIPTION_CHARS=30
SCORE_MIN_COMMENT_CHARS=20
SCORE_REVIEW_STATUSES=In Review,Code Review,Review
SCORE_INCIDENT_TYPES=Incident,Support
SCORE_INCIDENT_PROJECTS=
SCORE_MIN_MEDIAN_TASKS=2
SCORE_PROJECT_PREFIXES=         # blank = SUMMARY_CHANNEL_ROUTES + ARCHIVE_PROJECT_NAMES

SCORE_PARENT_DESCRIPTION=true   # a sub-task inherits its parent's description
SCORE_MEDIA_IS_COMMENT=true     # a screenshot-only comment counts as an update
SCORE_COMMIT_IN_COMMENT=true    # a commit id in a comment counts (no SCM linked)
SCORE_REQUIRE_ASSIGNEE=true     # the comment's 10 points need the task assigned to them

SCORE_CREDIT_DONE=1.0
SCORE_CREDIT_REVIEW_COMMIT=1.0
SCORE_CREDIT_REVIEW=0.5
SCORE_CREDIT_IN_PROGRESS=0.25
SCORE_CREDIT_TODO=0.0

SCORE_OPS_CHANNEL_ID=
SCORE_PUBLIC_SCORES=true      # post every developer's score to the check-in channel
SCORE_PUBLIC_ORDER=roster     # roster | score
SCORE_DM_ENABLED=false        # per-person DM with the full evidence breakdown
SCORE_NUDGE_ENABLED=true      # DM at capture time when no ticket id is named yet
```

During the shadow phase (§12) set `SCORE_PUBLIC_SCORES=false` as well — the
point of shadow mode is that nobody sees a score until the weights are tuned.

The six weights must sum to 100; the scorer refuses to run if they do not.

---

## 11. Worked examples

### 11.1 Partial credit

Raghul — Present, checked in, picked `SP-12`, `SP-15`, `HIR-72`. Trailing median
3 tasks.

| Check | Result | Points |
|-------|--------|--------|
| Check-in | ✓ | 10.0 |
| Task picked | ✓ | 5.0 |
| Description | SP-12 ✓, SP-15 ✓, HIR-72 ✗ → 2/3 | 6.7 |
| Commit ID | SP-12 (dev panel), SP-15 (comment) → 2/3 | 3.3 |
| Comment | SP-12 ✓, SP-15 ✓ → 2/3 | 6.7 |
| Delivery | SP-12 Done 1.0 · SP-15 review+commit 1.0 · HIR-72 in progress 0.25 → 2.25/3, `volume_factor` 1.0 | 45.0 |
| | **Total** | **76.7** — On Track |

### 11.2 Under-commitment

Same developer, trailing median 3, picks one task and completes it fully.

```
process        = 10 + 5 + 10 + 5 + 10 = 40
credit / n     = 1.0 / 1 = 1.0
volume_factor  = min(1.0, 1/3) = 0.33
delivery       = 60 × 1.0 × 0.33 = 20.0
total          = 60.0  — Needs Attention
```

Without §5.1 this day would score 100.

### 11.2b Work in review

Sahil — Present, checked in, eight sub-tasks: five in `Review`, one `In
Progress`, two `To Do`. Descriptions live on the parents; same-day comments on
five; no SCM linked.

```
description    = 10 × 8/8 = 10.0     (inherited from parents, §4.3)
commit         = 5 × 0/8  = 0.0      (no panel data, no commit ids in comments)
comment        = 10 × 5/8 = 6.25
process        = 10 + 5 + 10 + 0 + 6.25 = 31.2
credit         = 5×0.5 + 1×0.25 + 2×0 = 2.75
delivery       = 60 × 2.75/8 = 20.6
total          = 51.9  — Needs Attention
```

Under the original binary rule this same day scored **22.5**, with delivery 0.0 —
five tickets' worth of finished work awaiting review counted for nothing.

### 11.3 Absent

```
attendance = Absent → total = 0, status = Scored, all check columns 0
```

### 11.4 Jira outage

```
attendance = Present, Jira unreachable → status = Not Scored, total blank
```

Excluded from averages; recomputed at 02:00.

---

## 12. Rollout

| Phase | Duration | Gate |
|-------|----------|------|
| Backfill | — | Score 3 past weeks from existing sheet + Jira data; weights tuned here, once |
| Shadow | 2 weeks | Rows written, `SCORE_DM_ENABLED=false`, nobody notified. Distribution is sane — not everyone at 95, not everyone at 40 |
| Live | — | DMs on, dashboard published |

The rubric is published to the team **before** the live phase. Nobody is measured
against a rule they have not read.

---

## 13. Known limitations

State these plainly rather than discovering them later.

* **Process points saturate.** Checks 1–5 measure whether artefacts exist, not
  their quality. Expect the 40-point process score to sit near maximum for
  everyone within a few weeks. It is a compliance floor, not a differentiator —
  the delivery score is the number that will actually vary, which is why §8.2
  charts them separately.
* **All tasks weigh the same.** A one-line CSS fix and a week-long migration each
  count as one task. §5.1 limits the damage from under-committing but does not
  measure task size. Story-point weighting is the natural v2 if the team
  estimates consistently.
* **Description quality is unmeasured.** Only length is checked (§4.3), and a
  sub-task can now inherit a parent description written by someone else.
* **A ticket can sit in review indefinitely at 50%.** Nothing ages the review
  credit down, so parked work keeps paying. Detecting it needs time-in-status
  from the Jira changelog and is deferred with the other changelog items below.
* **The commit check is currently near-decorative.** With no SCM connected, it
  can only be satisfied by pasting a commit id into a comment, which most people
  will not do. Expect it to score near zero until GitHub is linked to Jira.
* **Ticket splitting is unguarded in v1.** Splitting one task into five inflates
  `n`, which raises `volume_factor` and can raise `done_ratio`. Detecting this
  needs the Jira changelog and is deferred.
* **Reopened work keeps its credit.** An issue marked Done at the cutoff and
  reopened the next day is not clawed back in v1. This also needs the changelog.
* **A score is not a performance review.** It measures adherence to a stand-up
  and ticketing process. It does not measure code quality, mentoring, design
  work, incident response beyond §7.1, or anything done outside Jira.

---

## 14. Non-goals

* The reasoning engine does not compute, adjust, or veto any score.
* No manual override, no appeals workflow, no manager-entered values. Correction
  happens only through §7.2 recompute.
* No score is computed *relative* to other developers. The under-commitment
  guard (§5.1) compares a developer only to their own trailing median, and
  nothing else in the rubric reads another person's numbers. Scores are
  published together (§8.3), but they are not competitive with one another —
  everyone can score 100 on the same day.
