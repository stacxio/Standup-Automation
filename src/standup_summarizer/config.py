"""Load settings and secrets from the environment; expose typed config.

All credentials are read from env / secret files — never hardcoded (NFR-5).
Only the Slack settings are defined here so far; reasoning-engine and Sheets
config will be added alongside their modules.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None, *, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_routes(raw: str | None) -> dict[str, str]:
    """Parse 'SP:C0AAA,WS:C0AAA,HIR:C0BBB' into {prefix_upper: channel}.

    Blank entries and malformed pairs (missing ':') are skipped so a typo in
    one route never breaks the whole run. Prefixes are upper-cased so matching
    is case-insensitive; channel ids/names are taken verbatim.
    """
    routes: dict[str, str] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        prefix, channel = part.split(":", 1)
        prefix, channel = prefix.strip().upper(), channel.strip()
        if prefix and channel:
            routes[prefix] = channel
    return routes


@dataclass(frozen=True)
class SlackConfig:
    """Settings for the fetch stage (SRS Section 9 — Slack).

    `channel_id` is the default channel (SLACK_CHANNEL_ID — #stacx-check-in);
    it is where fetches read from and where any summary slice with no matching
    route falls back to. `channel_routes` maps an issue-key prefix (e.g. "HIR")
    to the coordination channel that prefix's summary should post to.
    """

    bot_token: str
    channel_id: str
    include_threads: bool = True
    channel_routes: dict[str, str] = field(default_factory=dict)
    developer_routes: dict[str, str] = field(default_factory=dict)
    """Developer short name -> the channel their whole update posts to. Takes
    precedence over `channel_routes`: some people work to one team's channel
    whatever the issue-key prefix says, and a task-less developer must not fall
    back into the check-in channel."""

    @classmethod
    def from_env(cls) -> "SlackConfig":
        return cls(
            bot_token=_env("SLACK_BOT_TOKEN", required=True),  # type: ignore[arg-type]
            channel_id=_env("SLACK_CHANNEL_ID", required=True),  # type: ignore[arg-type]
            include_threads=_env_bool("SLACK_INCLUDE_THREADS", True),
            channel_routes=_parse_routes(_env("SUMMARY_CHANNEL_ROUTES")),
            developer_routes=_parse_routes(_env("SUMMARY_DEVELOPER_ROUTES")),
        )

    def channel_for_prefix(self, prefix: str) -> str:
        """Channel for an issue-key prefix, or the default channel if unrouted."""
        return self.channel_routes.get((prefix or "").upper(), self.channel_id)

    def channel_for_developer(self, name: str) -> str | None:
        """Channel this developer's whole update posts to, or None if unrouted."""
        return self.developer_routes.get((name or "").strip().upper())


@dataclass(frozen=True)
class JiraConfig:
    """Atlassian Cloud REST credentials, used to check issue status categories.

    Optional: from_env() returns None when any of the three vars is missing, so
    the report still runs (Picked Tasks stays populated from Slack; Completed
    Tasks is simply left blank).
    """

    base_url: str
    email: str
    api_token: str

    @classmethod
    def from_env(cls) -> "JiraConfig | None":
        base = _env("JIRA_BASE_URL")
        email = _env("JIRA_EMAIL")
        token = _env("JIRA_API_TOKEN")
        if not (base and email and token):
            return None
        return cls(base_url=base.rstrip("/"), email=email, api_token=token)


@dataclass(frozen=True)
class OtterConfig:
    """How the archive agent gets meetings out of Otter.

    Otter exposes two very different doors, and they suit different plans:

      * ``official`` — the Otter **Public API** (Bearer key). Documented and
        stable, but only enabled for Enterprise workspaces: ask your account
        manager, then Integrations -> Developer -> Create key.
      * ``web`` — the internal API the otter.ai web app itself calls, driven
        with your account email/password. Works on any plan, but it is
        unofficial and Otter can change or block it without notice.

    Both base URLs are configuration rather than code, so if Otter moves an
    endpoint the fix is a `.env` edit. `from_env()` returns None when the
    selected backend has no credentials — the archive agent then falls back to
    the inbox source (files you export by hand), which needs no login at all.
    """

    backend: str  # official | web
    api_key: str | None = None
    email: str | None = None
    password: str | None = None
    api_base: str = "https://api.otter.ai/v1"
    web_base: str = "https://otter.ai/forward/api/v1"
    workspace_id: str | None = None

    @classmethod
    def from_env(cls) -> "OtterConfig | None":
        backend = (_env("OTTER_BACKEND", "official") or "official").strip().lower()
        api_key = _env("OTTER_API_KEY")
        email, password = _env("OTTER_EMAIL"), _env("OTTER_PASSWORD")
        if backend == "official" and not api_key:
            return None
        if backend == "web" and not (email and password):
            return None
        if backend not in {"official", "web"}:
            return None
        return cls(
            backend=backend,
            api_key=api_key,
            email=email,
            password=password,
            api_base=(_env("OTTER_API_BASE", "https://api.otter.ai/v1") or "").rstrip("/"),
            web_base=(_env("OTTER_WEB_BASE", "https://otter.ai/forward/api/v1") or "").rstrip("/"),
            workspace_id=_env("OTTER_WORKSPACE_ID"),
        )


@dataclass(frozen=True)
class ArchiveConfig:
    """Where the meeting archive lives in Drive and how meetings map to projects.

    `project_names` maps a Jira issue-key prefix to the archive's project folder
    (several prefixes may share one, exactly like SUMMARY_CHANNEL_ROUTES routes
    SP and WS to the same channel). A meeting whose transcript names no known
    prefix is filed under `default_project`.
    """

    root_name: str = "Meeting Archive"
    root_folder_id: str | None = None
    project_names: dict[str, str] = field(default_factory=dict)
    default_project: str = "General"
    keep_audio: bool = True

    @classmethod
    def from_env(cls) -> "ArchiveConfig":
        return cls(
            root_name=_env("DRIVE_ARCHIVE_FOLDER", "Meeting Archive"),  # type: ignore[arg-type]
            root_folder_id=(_env("DRIVE_ARCHIVE_FOLDER_ID") or None),
            project_names=_parse_routes(_env("ARCHIVE_PROJECT_NAMES")),
            default_project=_env("ARCHIVE_DEFAULT_PROJECT", "General"),  # type: ignore[arg-type]
            keep_audio=_env_bool("ARCHIVE_KEEP_AUDIO", True),
        )

    def project_for_prefix(self, prefix: str) -> str | None:
        """Archive project folder for an issue-key prefix, or None if unrouted."""
        return self.project_names.get((prefix or "").upper())


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(_env(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


def _env_set(name: str, default: frozenset[str]) -> frozenset[str]:
    """Parse 'In Review,Code Review' into a set; blank falls back to `default`."""
    parts = {p.strip() for p in (_env(name) or "").split(",") if p.strip()}
    return frozenset(parts) if parts else default


@dataclass(frozen=True)
class ScoreConfig:
    """Daily performance scoring — see docs/SCORING.md §10.

    Every weight and threshold is configuration rather than a constant, because
    they are tuned once against a backfill before the system goes live. The six
    weights must sum to 100; `scorecard.Weights.validate()` enforces that on
    every run so a mistyped `.env` fails loudly instead of quietly scoring
    everyone out of 95.
    """

    weights: "scorecard.Weights"
    thresholds: "scorecard.Thresholds"
    capture_hour: int = 11
    cutoff_hour: int = 18
    recompute_days: int = 3
    ops_channel_id: str | None = None
    dm_enabled: bool = False
    public_scores: bool = True
    """Post every developer's score to the check-in channel each day. When
    false, only the team aggregate is posted and individual scores go by DM."""
    public_order: str = "roster"
    """'roster' (roll-call order) or 'score' (highest first)."""
    project_prefixes: frozenset[str] = frozenset()
    """Issue-key prefixes that name a real Jira project. Empty means "derive
    them from SUMMARY_CHANNEL_ROUTES and ARCHIVE_PROJECT_NAMES", which is what
    every other agent already routes on."""

    @classmethod
    def from_env(cls) -> "ScoreConfig":
        from . import scorecard  # local import: config must not depend on rules

        weights = scorecard.Weights(
            checkin=_env_float("SCORE_WEIGHT_CHECKIN", 10.0),
            picked=_env_float("SCORE_WEIGHT_PICKED", 5.0),
            description=_env_float("SCORE_WEIGHT_DESCRIPTION", 10.0),
            commit=_env_float("SCORE_WEIGHT_COMMIT", 5.0),
            comment=_env_float("SCORE_WEIGHT_COMMENT", 10.0),
            done=_env_float("SCORE_WEIGHT_DONE", 60.0),
        )
        weights.validate()
        thresholds = scorecard.Thresholds(
            min_description_chars=_env_int("SCORE_MIN_DESCRIPTION_CHARS", 30),
            min_comment_chars=_env_int("SCORE_MIN_COMMENT_CHARS", 20),
            review_statuses=_env_set("SCORE_REVIEW_STATUSES",
                                     frozenset({"In Review", "Code Review", "Review"})),
            incident_types=_env_set("SCORE_INCIDENT_TYPES",
                                    frozenset({"Incident", "Support"})),
            incident_projects=_env_set("SCORE_INCIDENT_PROJECTS", frozenset()),
            min_median_tasks=_env_float("SCORE_MIN_MEDIAN_TASKS", 2.0),
            parent_description_fallback=_env_bool("SCORE_PARENT_DESCRIPTION", True),
            media_counts_as_comment=_env_bool("SCORE_MEDIA_IS_COMMENT", True),
            commit_in_comment_counts=_env_bool("SCORE_COMMIT_IN_COMMENT", True),
            credit_done=_env_float("SCORE_CREDIT_DONE", 1.0),
            credit_review_with_commit=_env_float("SCORE_CREDIT_REVIEW_COMMIT", 1.0),
            credit_review=_env_float("SCORE_CREDIT_REVIEW", 0.5),
            credit_in_progress=_env_float("SCORE_CREDIT_IN_PROGRESS", 0.25),
            credit_todo=_env_float("SCORE_CREDIT_TODO", 0.0),
        )
        return cls(
            weights=weights,
            thresholds=thresholds,
            capture_hour=_env_int("SCORE_CAPTURE_HOUR", 11),
            cutoff_hour=_env_int("SCORE_CUTOFF_HOUR", 18),
            recompute_days=_env_int("SCORE_RECOMPUTE_DAYS", 3),
            ops_channel_id=(_env("SCORE_OPS_CHANNEL_ID") or None),
            dm_enabled=_env_bool("SCORE_DM_ENABLED", False),
            public_scores=_env_bool("SCORE_PUBLIC_SCORES", True),
            public_order=(_env("SCORE_PUBLIC_ORDER", "roster") or "roster").strip().lower(),
            project_prefixes=frozenset(
                p.strip().upper() for p in (_env("SCORE_PROJECT_PREFIXES") or "").split(",")
                if p.strip()
            ),
        )

    def known_prefixes(self, *fallbacks: dict) -> frozenset[str]:
        """Configured prefixes, else those the other agents already route on.

        Stand-up text is hand-typed prose, and the key parser matches anything
        shaped like `ABC-123` — "ST-11", "PI-01" and "ORG-404" all came out of
        real stand-ups and are not Jira issues. An unresolvable key sends the
        whole developer-day to `Not Scored`, so an unfiltered parse quietly
        costs people scored days.
        """
        if self.project_prefixes:
            return self.project_prefixes
        found: set[str] = set()
        for mapping in fallbacks:
            found |= {str(k).upper() for k in (mapping or {})}
        return frozenset(found)


@dataclass(frozen=True)
class ReasoningConfig:
    """Settings for the summarize stage's reasoning engine (SRS Section 8 / 9).

    backend selects the engine; everything else is config, not code (FR-11):
      * "local"  / "openai" / "dashscope" -> OpenAI-compatible endpoint
      * "anthropic"                        -> Anthropic Messages API
    """

    backend: str
    model: str
    base_url: str | None = None
    api_key: str | None = None
    max_retries: int = 3

    @classmethod
    def from_env(cls) -> "ReasoningConfig":
        return cls(
            backend=(_env("REASONING_BACKEND", "local") or "local").lower(),
            model=_env("REASONING_MODEL", "llama3.2"),  # type: ignore[arg-type]
            base_url=_env("REASONING_BASE_URL", "http://localhost:11434/v1"),
            api_key=_env("REASONING_API_KEY"),
            max_retries=int(_env("REASONING_MAX_RETRIES", "3") or 3),
        )
