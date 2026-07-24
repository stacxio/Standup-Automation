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
