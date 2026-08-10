"""Archive agent glue: spoken Jira keys, project->channel routing, config."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import archive_meetings as am  # noqa: E402
from standup_summarizer.config import ArchiveConfig, OtterConfig, SlackConfig  # noqa: E402

PREFIXES = {"SP", "WS", "HIR", "BHA"}
ARCH = ArchiveConfig(project_names={"SP": "STACX", "WS": "STACX", "HIR": "HIROCOM", "BHA": "BHA"})
SLACK = SlackConfig(
    bot_token="xoxb-test", channel_id="C-checkin",
    channel_routes={"SP": "C-stacx", "WS": "C-stacx", "HIR": "C-hiro", "BHA": "C-bha"},
)


# --- spoken Jira keys -----------------------------------------------------
def test_extract_keys_handles_spoken_hyphenless_ids():
    text = "I finished SP-12, picked up SP 19 and reviewed HIR7. Blocker on BHA - 4."
    assert am.extract_keys(text, PREFIXES) == ["SP-12", "SP-19", "HIR-7", "BHA-4"]


def test_extract_keys_order_follows_the_transcript_not_the_prefix_set():
    """Order must not depend on set iteration — detect_project breaks ties on it."""
    text = "BHA-4 first, then HIR-7, then SP-12."
    assert am.extract_keys(text, PREFIXES) == ["BHA-4", "HIR-7", "SP-12"]
    assert am.extract_keys(text, {"SP", "HIR", "BHA"}) == ["BHA-4", "HIR-7", "SP-12"]


def test_extract_keys_strips_leading_zeros_and_dedupes():
    assert am.extract_keys("SP 007 and SP-7 again", PREFIXES) == ["SP-7"]


def test_extract_keys_ignores_prose_and_unknown_prefixes():
    assert am.extract_keys("we met at 3:30 and reviewed page - 2", PREFIXES) == []
    assert am.extract_keys("ZZ 14 was mentioned", PREFIXES) == []


def test_extract_keys_is_case_insensitive_for_spoken_ids():
    assert am.extract_keys("closed sp 12 today", PREFIXES) == ["SP-12"]


def test_extract_keys_tolerates_empty_text():
    assert am.extract_keys("", PREFIXES) == []


# --- project -> Slack channel --------------------------------------------
def test_project_routes_through_its_issue_prefixes():
    assert am.channel_for_project(SLACK, ARCH, "STACX") == "C-stacx"
    assert am.channel_for_project(SLACK, ARCH, "HIROCOM") == "C-hiro"


def test_unrouted_project_falls_back_to_the_check_in_channel():
    assert am.channel_for_project(SLACK, ARCH, "General") == "C-checkin"


def test_project_with_a_mapping_but_no_slack_route_falls_back():
    arch = ArchiveConfig(project_names={"QA": "QUALITY"})
    assert am.channel_for_project(SLACK, arch, "QUALITY") == "C-checkin"


# --- config ---------------------------------------------------------------
def test_archive_config_parses_prefix_mapping(monkeypatch):
    monkeypatch.setenv("ARCHIVE_PROJECT_NAMES", "sp:STACX, HIR:HIROCOM ,broken")
    monkeypatch.setenv("ARCHIVE_KEEP_AUDIO", "false")
    cfg = ArchiveConfig.from_env()
    assert cfg.project_names == {"SP": "STACX", "HIR": "HIROCOM"}  # malformed pair skipped
    assert cfg.project_for_prefix("sp") == "STACX"
    assert cfg.project_for_prefix("zz") is None
    assert cfg.keep_audio is False
    assert cfg.root_name == "Meeting Archive" and cfg.default_project == "General"


def test_otter_config_requires_credentials_for_the_chosen_backend(monkeypatch):
    for var in ("OTTER_BACKEND", "OTTER_API_KEY", "OTTER_EMAIL", "OTTER_PASSWORD",
                "OTTER_API_BASE", "OTTER_WEB_BASE", "OTTER_WORKSPACE_ID"):
        monkeypatch.delenv(var, raising=False)
    assert OtterConfig.from_env() is None  # official backend, no key

    monkeypatch.setenv("OTTER_API_KEY", "sk-otter")
    cfg = OtterConfig.from_env()
    assert cfg.backend == "official" and cfg.api_base == "https://api.otter.ai/v1"

    monkeypatch.setenv("OTTER_BACKEND", "web")
    assert OtterConfig.from_env() is None  # web backend needs email + password
    monkeypatch.setenv("OTTER_EMAIL", "me@example.com")
    monkeypatch.setenv("OTTER_PASSWORD", "pw")
    assert OtterConfig.from_env().backend == "web"

    monkeypatch.setenv("OTTER_BACKEND", "carrier-pigeon")
    assert OtterConfig.from_env() is None


def test_otter_config_trims_trailing_slashes_from_bases(monkeypatch):
    monkeypatch.setenv("OTTER_API_KEY", "sk-otter")
    monkeypatch.setenv("OTTER_BACKEND", "official")
    monkeypatch.setenv("OTTER_API_BASE", "https://api.otter.ai/v1/")
    assert OtterConfig.from_env().api_base == "https://api.otter.ai/v1"
