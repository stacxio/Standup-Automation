"""Orchestrator / cron entry point (FR-17..FR-20).

- Compute the run window from configured timezone + window definition (FR-17).
- Execute fetch -> summarize -> push in order, halting with a clear error if a
  stage fails irrecoverably (FR-18).
- Runnable on a schedule and manually for a given date / backfill (FR-19).
- Structured logs that never expose secrets (FR-20, NFR-6).
"""


def main() -> None:
    raise NotImplementedError


if __name__ == "__main__":
    main()
