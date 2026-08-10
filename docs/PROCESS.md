# Stand-up Automation — Process & Agent Guide

This document describes **which agent (script/module) does what** in the pipeline,
what triggers it, what it reads, and what it produces. "Agent" here means a
runnable component; the reasoning engine (LLM) is the only true AI agent, invoked
by the summarize / gap / verify agents.

---

## 1. Data sources & sinks

| Kind | What |
|------|------|
| **Slack — #stacx-check-in** | Developers post roll-calls (`GN-Present`), daily stand-ups, and leave messages. This is the primary input channel and the summary fallback. |
| **Slack — #feedback-stacx / #feedback-hirocom / #feedback-bha** | Per-project routing targets for the daily digest. |
| **Jira (stacx24team.atlassian.net)** | Issue status, description, comments, assignee, priority, attachments, and dev-panel branch/commit/PR. |
| **Otter.ai export** | Google Meet stand-up transcript, supplied as a `.txt` export/paste (method A — no login/scrape). Used by the two audit agents. |
| **Otter.ai API** | Recordings, transcripts and AI summaries pulled by the archive agent — Public API (Bearer key, Enterprise) or the unofficial web API, with a hand-export inbox as fallback. |
| **Google Sheets — Attendance** (`1W3H2u…4sOI`) | Master, Summary, month tabs, Leaves. |
| **Google Sheets — Daily Status** (`1j87qp…4ZzM`) | Month tabs, Gap Report, Standup Verification, Meeting Archive. |
| **Google Drive** | Payslip PDFs (optional upload); the **meeting archive** (recordings, transcripts, summaries, notes) under `DRIVE_ARCHIVE_FOLDER`. |

All credentials and channel IDs live in the **gitignored `.env`** (`.env.example`
documents every variable).

---

## 2. Agents

### 2.1 Daily pipeline agents (run by the scheduler)

#### `run_daily.py` — Orchestrator
- **Role:** the Windows scheduled task's entry point. Runs the three daily agents
  in order as subprocesses and appends combined output to `logs/daily.log`.
- **Steps:** `build_attendance.py --notify` → `build_report.py --notify` → `daily_summary.py`.
- **Exit convention:** exit 2 from a step = "skipped" (e.g. no reasoning backend),
  not a failure.
- **Note:** does **not** run `verify_standup.py` / `gap_report.py` — those need a
  transcript and stay on-demand.

#### `build_attendance.py` — Attendance agent
- **Reads:** #stacx-check-in roll-call posts + leave messages.
- **Writes:** the Attendance sheet — Master (seeded once), Summary (per-person
  absence), one matrix tab per month (Present/Absent/Half Day/Weekend/Leave).
- **Slack:** posts "attendance recorded" only if a roll-call exists for today.

#### `build_report.py` — Daily Status report agent
- **Reads:** last 30 days of Slack stand-ups → **reasoning engine** summarizes each
  into structured columns → **Jira** for live per-issue status.
- **Writes:** the Daily Status sheet, one tab per month; derives Picked/Completed
  Tasks from the Jira keys mentioned (tolerates hand-typed `HIR - 72` / `BHA- 79`).
- **Slack:** posts "Stand-up updated successfully!" after the sheet is written.

#### `daily_summary.py` — Per-developer digest agent (routed)
- **Reads:** today's check-ins (attendance) + Jira ids per developer (report) +
  **Jira** detail (summary, description, branch/commit/PR, comments).
- **Routing:** splits each developer's tasks by **issue-key prefix** and posts the
  slice to that project's channel via `SUMMARY_CHANNEL_ROUTES`
  (SP/WS → #feedback-stacx, HIR → #feedback-hirocom, BHA → #feedback-bha).
  Unrouted prefixes and task-less developers fall back to #stacx-check-in.
- **Flags:** posts on every run; `--dry-run` prints without posting.

### 2.2 On-demand audit agents (need the meeting transcript)

#### `gap_report.py` — Slack-vs-transcript gap agent
- **Input:** Otter transcript (`--transcript` / `--stdin` / `--text`).
- **Reads:** today's Slack stand-ups + the transcript → **reasoning engine**
  compares them.
- **Produces per developer:** `in_slack` / `in_meeting`, an assessment (aligned /
  minor / significant gaps / no update), and specific discrepancies.
- **Writes:** `Gap Report` tab on the Daily Status sheet; posts to #stacx-check-in.

#### `verify_standup.py` — Jira-vs-transcript verification agent (Scrum Master)
- **Input:** Otter transcript; optional `--developer NAME` for a single-person
  check-in (attributes the whole transcript to that developer).
- **Reads:** each developer's active assigned **Jira** issues (summary, status,
  priority, description/acceptance-criteria, comments, attachments, PR) plus any
  issue they name in the transcript → **reasoning engine** acts as Scrum Master.
- **Produces per developer:** per-issue checks (discussed / description / comments
  / acceptance-criteria / PR / attachments / blocker), a coverage %, overall
  status, work mentioned but not in Jira, and a recommendation.
- **Determinism guards:** `discussed` is set by whether the issue key is named in
  the transcript (not the model); assignee→roster and speaker→developer use loose
  name matching; hyphenless spoken IDs (`SP12`, `SP 12`) resolve to `SP-12`.
- **Writes:** `Standup Verification` tab on the Daily Status sheet; posts to
  #stacx-check-in.
- **Scope knobs:** `--since-days` (default 3), `--max-issues` (default 8).

### 2.3 Knowledge-retention agents

#### `archive_meetings.py` — Meeting archive agent
- **Role:** builds the team's durable, per-project record of meetings for
  knowledge sharing, onboarding, compliance and audit.
- **Input:** meetings from Otter via one of three sources (`OTTER_BACKEND`):
  `official` (Public API, Bearer key, Enterprise workspaces), `web` (the
  unofficial internal API, email/password, any plan), or `--inbox DIR` (files
  you exported by hand — no login, cannot break).
- **Routing:** the project is inferred from the Jira issue keys spoken in the
  meeting via `ARCHIVE_PROJECT_NAMES` (prefix → project folder; several
  prefixes may share one). Most mentions wins, ties go to first mention,
  unmatched meetings land in `ARCHIVE_DEFAULT_PROJECT`. `--project` overrides.
- **Writes (Drive):** `<root>/<Project>/<Year>/<YYYY-MM-DD>_<slug>/` containing
  `recording.<ext>`, `transcript.txt` (Otter format, so the audit agents can
  consume it), `ai-summary.md`, `notes.md` and `meta.json`. Uses the **OAuth**
  Drive credentials, not the service account.
- **Writes (Sheets):** upserts one row per meeting into the `Meeting Archive`
  tab of the Daily Status sheet — the browsable index. It accumulates across
  runs rather than being replaced.
- **Slack:** posts the artifact links to the project's channel, reusing
  `SUMMARY_CHANNEL_ROUTES` rather than a second mapping.
- **Idempotency:** a meeting folder holding `meta.json` is skipped unless
  `--force`; `notes.md` is never overwritten once it exists, so team edits
  survive. Index rows upsert by meeting id. Jira keys are ordered by position
  in the transcript, so a re-run cannot silently re-file a meeting under a
  different project.
- **Flags:** `--since-days` (default 1), `--date`, `--project`, `--inbox`,
  `--limit` (default 25), `--no-audio`, `--force`, `--dry-run`.
- **Note:** on-demand / scheduled separately — `run_daily.py` does not run it.

### 2.4 Supporting / standalone agents

#### `leave_app.py` — Leave form agent (Socket Mode, long-running)
- **Trigger:** the `/leave` slash command opens a modal (From, To, Leave type).
- **Writes:** appends to the "Leaves" tab of the Attendance sheet; DMs a
  confirmation. Must stay running to accept submissions.

#### `build_salary_slips.py` — Payroll agent
- **Reads:** Master + Summary tabs of the Attendance sheet for a pay period
  (defaults to the previous month).
- **Writes:** an official STACX24 payslip PDF per employee into `Salary_Slips/`;
  `--upload` pushes them to Drive.

#### `drive_oauth.py` — Drive uploader (helper)
- **Role:** OAuth (user-credential) uploads to *your* Drive (the service account
  has no Drive storage). Used by the payroll agent's `--upload`.

---

## 3. Shared library — `src/standup_summarizer/`

These modules are the reusable machinery the agents call (not run directly):

| Module | Responsibility |
|--------|----------------|
| `config.py` | Typed settings from `.env` (Slack, Jira, reasoning, channel routes). |
| `fetch.py` | Slack channel history, user-name resolution, grouping. |
| `jira.py` | Jira REST reads: status, detail (assignee/priority/attachments), comments, dev-panel branch/commit/PR, and JQL search. |
| `engines/` | Pluggable **reasoning engine** — `local` (OpenAI-compatible/Ollama) or `hosted` (Anthropic). The only AI agent; called by summarize/gap/verify. |
| `summarize.py` | Turn stand-up notes into structured records (one batched engine call). |
| `transcript.py` | Parse an Otter export into `{speaker, text}` segments. |
| `otter.py` | Pull meetings from Otter — Public API / unofficial web API / inbox — normalised onto one `Meeting` shape. Renders transcripts back into Otter export format so `transcript.py` handles them unchanged. |
| `archive.py` | Pure filing rules for the archive: project detection, folder paths, the generated documents, index upsert, Slack notice. No I/O. |
| `gap.py` | Slack-vs-transcript comparison logic. |
| `verify.py` | Jira-vs-transcript Scrum-Master verification logic. |
| `sheets.py`, `models.py`, `run.py`, `cli.py` | Sheets upsert, data shapes, and a standalone fetch/summarize entry point. |

---

## 4. Daily flow (who runs when)

```
Windows Scheduled Task  (weekdays 18:00 IST)
      │
      ▼
run_daily.py ──► build_attendance.py ──► Attendance sheet + Slack notice
             ──► build_report.py     ──► Daily Status sheet + Slack notice
                     │  (reasoning engine + Jira)
             ──► daily_summary.py    ──► #feedback-stacx / #feedback-hirocom /
                     │  (Jira detail)      #feedback-bha  (routed by prefix)

On demand, after the Google Meet stand-up (you export the Otter transcript):
      │
      ▼
verify_standup.py --transcript otter.txt [--developer NAME]
      │  (Jira context + reasoning engine)
      ▼  Standup Verification tab + #stacx-check-in

gap_report.py --transcript otter.txt
      │  (Slack stand-ups + reasoning engine)
      ▼  Gap Report tab + #stacx-check-in

archive_meetings.py [--since-days N | --date D] [--inbox DIR]
      │  (Otter API or hand exports -> Drive, by project)
      ▼  Drive <Project>/<Year>/<date>_<slug>/ + Meeting Archive tab
      ▼  links posted to the project's channel
```

---

## 5. Operational notes

- **Scheduled task** `StacxAttendanceDaily`: weekdays 18:00 IST, 1-hour execution
  limit, battery guards off. Runs only while the user is logged in (interactive
  logon).
- **Transcript ingestion for the audit agents is method A** — you paste/export the
  Otter transcript; no Otter login or scraping. The archive agent is the one
  exception: it authenticates to Otter to pull recordings, which a paste cannot
  carry. `--inbox` keeps method A available there too.
- **Otter's Public API is Enterprise-gated**, and the `web` backend is unofficial
  and unversioned. Treat a sudden failure of either as expected rather than a
  bug, and fall back to `archive_meetings.py --inbox DIR`. Both base URLs are
  `.env` settings so an endpoint move needs no code change.
- **The archive holds meeting recordings of staff** — it inherits whatever Drive
  permissions its root folder has. Set `DRIVE_ARCHIVE_FOLDER_ID` to a folder
  shared with the team rather than widening a personal folder.
- **Acceptance criteria** are read from the Jira **description** (this Jira has no
  dedicated AC field).
- **Attachments** are matched by **filename only** (no PDF/Excel text extraction).
- **AI caveat:** the reasoning engine's per-issue judgements are review aids, not an
  audit of record. Facts that can be checked deterministically (who checked in,
  which issue was named, PR merged) are computed in code, not left to the model.
- **The bot must be a member** of every routed channel (`chat:write` only, no
  `channels:join`) — invite it with `/invite`.
