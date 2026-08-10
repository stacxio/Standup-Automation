# Daily Standup Summarizer

Automated pipeline that collects daily Slack standup messages, summarizes them
per person with a (pluggable) reasoning engine, and upserts the results into a
Google Sheet. Scheduled, non-interactive, one batched model call per run.

```
cron --> run.py --> fetch.py --> summarize.py --> sheets.py --> Google Sheet
                       |             |               |
                   Slack API   Reasoning API     Sheets API
```

## Modules (`src/standup_summarizer/`)

| Module            | Responsibility                                              | External service     |
|-------------------|------------------------------------------------------------|----------------------|
| `config.py`       | Load settings/secrets from env; expose typed config        | —                    |
| `models.py`       | Single source of truth for the summary record schema       | —                    |
| `fetch.py`        | Pull channel history, resolve user IDs, group by person    | Slack API            |
| `summarize.py`    | Grouped messages -> structured summaries                   | Reasoning API        |
| `engines/`        | Pluggable reasoning backends (hosted / local)              | Reasoning API        |
| `sheets.py`       | Read existing rows; upsert summary rows                    | Google Sheets API    |
| `run.py`          | Orchestrate pipeline; compute run window; cron entry point | —                    |

## Phased delivery (Section 10 of the SRS)

1. **Summarize** — `summarize.py` + schema produce valid records from fixtures.
2. **Fetch** — `fetch.py` returns grouped, name-resolved messages from the live channel.
3. **Push** — `sheets.py` writes and upserts (idempotent re-runs).
4. **Schedule** — `run.py` ties stages together for cron + manual backfill.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
cp .env.example .env            # fill in secrets (never commit)
```

## Run

```bash
python -m standup_summarizer.run               # today's standup
python -m standup_summarizer.run --date 2026-06-17   # manual backfill
```

### Daily summary

`daily_summary.py` posts a per-developer digest (check-in status, picked/done
tasks, and each Jira issue's summary/description/branch/commit/PR + comments).

Each developer's tasks are routed by **issue-key prefix** to a project channel
via `SUMMARY_CHANNEL_ROUTES` (e.g. `SP:C…,WS:C…,HIR:C…,BHA:C…`); several
prefixes may share a channel. Anything unrouted — an unlisted prefix, or a
developer who referenced no task — falls back to `SLACK_CHANNEL_ID`. The bot
must be a member of every target channel.

```bash
python daily_summary.py            # post to the routed channels
python daily_summary.py --dry-run  # print the per-channel routing, post nothing
```

### Stand-up gap audit

`gap_report.py` cross-checks what developers wrote in Slack against what was
actually said in the Google Meet stand-up. You supply the meeting transcript as
an Otter text export (or pasted text) — no Otter login or scrape. It fetches
today's Slack stand-ups, asks the reasoning engine to compare the two, writes a
`Gap Report` tab to the Daily Status sheet, and posts a summary to
`SLACK_CHANNEL_ID` (#stacx-check-in).

```bash
python gap_report.py --transcript meeting.txt            # audit + push + post
python gap_report.py --transcript meeting.txt --dry-run  # print only
python gap_report.py --stdin < meeting.txt               # piped transcript
python gap_report.py --transcript meeting.txt --date 2026-07-28   # backfill
```

Per developer it reports `in_slack` / `in_meeting`, an assessment (aligned /
minor gaps / significant gaps / no update), and the specific discrepancies —
e.g. a blocker raised verbally but not written, or a Slack task never discussed.

### Meeting archive

`archive_meetings.py` builds the team's historical record of meetings in Google
Drive, organised by project — for knowledge sharing, onboarding, and audit.

```
<DRIVE_ARCHIVE_FOLDER>/<Project>/<Year>/<YYYY-MM-DD>_<slug>/
    recording.mp3     the Otter audio
    transcript.txt    Otter-format text (feeds gap_report / verify_standup)
    ai-summary.md     Otter's AI summary, outline, insights, action items
    notes.md          discussion notes — pre-filled stub, the team edits it
    meta.json         machine-readable record + the "already archived" marker
```

The project is inferred from the Jira issue keys spoken in the meeting, using
`ARCHIVE_PROJECT_NAMES` (e.g. `SP:STACX,WS:STACX,HIR:HIROCOM,BHA:BHA`); the
project with the most mentions wins, ties go to whichever was mentioned first,
and a meeting naming no known prefix lands in `ARCHIVE_DEFAULT_PROJECT`.
`--project` overrides the detection. Every project a meeting touched is recorded
in its `also_mentions`.

Each run upserts a row per meeting into the **Meeting Archive** tab of the Daily
Status sheet (date, project, title, duration, attendees, issues, and a link to
each artifact) and posts the links to that project's Slack channel — reusing the
`SUMMARY_CHANNEL_ROUTES` mapping rather than a second one.

```bash
python archive_meetings.py                    # today
python archive_meetings.py --since-days 7     # last week
python archive_meetings.py --date 2026-08-05
python archive_meetings.py --inbox exports/   # hand-exported files, no Otter login
python archive_meetings.py --dry-run          # print the plan, upload nothing
python archive_meetings.py --no-audio         # text only
```

**Getting meetings out of Otter.** Three routes, set by `OTTER_BACKEND`:

| Route | Auth | Availability |
|-------|------|--------------|
| `official` | `OTTER_API_KEY` (Bearer) | Otter's **Public API** — Enterprise workspaces only. Ask your account manager to enable it, then Integrations → Developer → Create key. |
| `web` | `OTTER_EMAIL` / `OTTER_PASSWORD` | The internal API the otter.ai web app calls. Any plan, but **unofficial** — Otter can change or block it without notice. |
| `--inbox DIR` | none | Files you exported from Otter by hand. Always works; the fallback when either API is unavailable. |

`OTTER_API_BASE` / `OTTER_WEB_BASE` are configuration, so an endpoint move is a
`.env` edit rather than a code change.

Re-runs are idempotent: a meeting folder that already contains `meta.json` is
skipped unless you pass `--force`, and `notes.md` is never overwritten once it
exists — the team's edits are safe.

Uploads use the OAuth Drive credentials (`drive_oauth.py auth`), not the service
account, which has no Drive storage of its own.

### Stand-up verification (Jira vs meeting)

`verify_standup.py` is the Scrum-Master audit: instead of Slack, it compares each
developer's **Jira context** against the meeting. For every roster developer it
collects their active assigned issues (summary, status, priority, description
with acceptance criteria, latest comments, attachment names, pull requests) plus
any issue they name in their transcript turn, then asks the engine to verify
coverage.

```bash
python verify_standup.py --transcript meeting.txt            # audit + push + post
python verify_standup.py --transcript meeting.txt --dry-run  # print only
python verify_standup.py --transcript m.txt --since-days 5 --max-issues 10
```

Per issue it checks discussed / description / comments / acceptance-criteria /
PR / attachments / blocker, then gives a coverage score, overall status, work
mentioned that isn't in Jira, and a recommendation. It writes a `Standup
Verification` tab to the Daily Status sheet and posts to `SLACK_CHANNEL_ID`.
Scope: assigned issues that aren't Done and were updated within `--since-days`
(default 3), capped at `--max-issues` (default 8), plus any issue named in the
meeting. Acceptance criteria are read from the description (no dedicated field);
attachments are matched by filename only.
