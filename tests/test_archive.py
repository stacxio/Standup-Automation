"""Meeting-archive filing rules and Otter payload normalisation (offline)."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from standup_summarizer import archive, otter, transcript  # noqa: E402

PROJECTS = {"SP": "STACX", "WS": "STACX", "HIR": "HIROCOM", "BHA": "BHA"}


def meeting(**kw) -> otter.Meeting:
    base = dict(
        id="conv-1",
        title="Daily Stand-up",
        started=dt.datetime(2026, 8, 6, 9, 30).astimezone(),
        duration_sec=930,
        attendees=["Soma Pani", "Raghul"],
        transcript="Soma Pani  0:03\nWorking on SP-12.\n",
        summary="The team discussed SP-12.",
        action_items=["Soma to close SP-12"],
        source="otter:official",
    )
    base.update(kw)
    return otter.Meeting(**base)


# --- slugs & paths --------------------------------------------------------
def test_slugify_is_safe_and_bounded():
    assert archive.slugify("Daily Stand-up — 9:30 AM!") == "daily-stand-up-9-30-am"
    assert archive.slugify("") == "meeting"
    assert len(archive.slugify("x" * 200)) <= 60
    assert not archive.slugify("--Hello--").startswith("-")


def test_folder_segments_are_project_year_dateslug():
    segs = archive.folder_segments("STACX", dt.date(2026, 8, 6), "Daily Stand-up")
    assert segs == ["STACX", "2026", "2026-08-06_daily-stand-up"]


# --- project detection ----------------------------------------------------
def test_dominant_project_wins():
    project, mentioned = archive.detect_project(["SP-12", "HIR-7", "SP-9"], PROJECTS)
    assert project == "STACX"
    assert mentioned == ["STACX", "HIROCOM"]  # first-appearance order


def test_prefixes_sharing_a_project_are_summed():
    # SP and WS both map to STACX, so two mentions beat HIROCOM's one.
    project, _ = archive.detect_project(["HIR-1", "SP-2", "WS-3"], PROJECTS)
    assert project == "STACX"


def test_tie_goes_to_the_first_mentioned():
    project, _ = archive.detect_project(["HIR-1", "SP-2"], PROJECTS)
    assert project == "HIROCOM"


def test_unmapped_prefixes_are_ignored_and_default_applies():
    project, mentioned = archive.detect_project(["ZZZ-1", "QA-4"], PROJECTS, default="General")
    assert project == "General" and mentioned == []
    assert archive.detect_project([], PROJECTS)[0] == "General"


# --- generated documents --------------------------------------------------
def test_summary_doc_states_the_facts_and_action_items():
    doc = archive.summary_doc(meeting(), "STACX", ["SP-12"])
    assert doc.startswith("# Daily Stand-up")
    assert "2026-08-06" in doc and "15:30" in doc  # duration 930s
    assert "**Project:** STACX" in doc
    assert "SP-12" in doc and "Soma Pani, Raghul" in doc
    assert "- [ ] Soma to close SP-12" in doc


def test_summary_doc_says_so_when_otter_gave_nothing():
    doc = archive.summary_doc(meeting(summary="", action_items=[]), "STACX", [])
    assert "no AI summary" in doc
    assert "## Action items" not in doc


def test_notes_doc_has_stable_headings_for_the_team_to_fill():
    doc = archive.notes_doc(meeting(), "STACX", ["SP-12"])
    for heading in ("## Decisions", "## Discussion", "## Follow-ups", "## Related Jira issues"):
        assert heading in doc
    assert "- [ ] Soma to close SP-12" in doc
    assert "never" in doc  # the "not overwritten by a later run" promise


def test_notes_doc_degrades_without_attendees_or_issues():
    doc = archive.notes_doc(meeting(attendees=[], action_items=[]), "General", [])
    assert "_to fill in_" in doc and "_none referenced_" in doc


# --- meta + index ---------------------------------------------------------
def meta_for(project="STACX", mid="conv-1", date="2026-08-06", mentioned=None) -> dict:
    return archive.build_meta(
        meeting(id=mid, started=dt.datetime.fromisoformat(f"{date}T09:30").astimezone()),
        project, ["SP-12"],
        {"folder": {"url": "https://drive/folder"},
         archive.RECORDING: {"url": "https://drive/rec"},
         archive.TRANSCRIPT: {"url": "https://drive/txt"}},
        projects=mentioned if mentioned is not None else [project, "HIROCOM"],
        archived_at=dt.datetime(2026, 8, 6, 18, 0).astimezone(),
    )


def test_build_meta_excludes_the_primary_from_also_mentions():
    meta = meta_for()
    assert meta["project"] == "STACX"
    assert meta["also_mentions"] == ["HIROCOM"]
    assert meta["jira_keys"] == ["SP-12"] and meta["duration"] == "15:30"
    assert meta["date"] == "2026-08-06" and meta["schema"] == 1


def test_index_row_matches_the_header_layout():
    row = archive.index_row(meta_for())
    assert len(row) == len(archive.INDEX_HEADERS)
    assert row[archive.DATE_COLUMN] == "2026-08-06"
    assert row[archive.ID_COLUMN] == "conv-1"
    assert row[archive.INDEX_HEADERS.index("Project")] == "STACX"
    assert row[archive.INDEX_HEADERS.index("Recording")] == "https://drive/rec"
    assert row[archive.INDEX_HEADERS.index("Summary")] == ""  # no link supplied


def test_merge_index_upserts_by_id_and_sorts_newest_first():
    old = [archive.index_row(meta_for(mid="a", date="2026-08-01")),
           archive.index_row(meta_for(mid="b", date="2026-08-05"))]
    refreshed = archive.index_row(meta_for(mid="a", date="2026-08-01", project="HIROCOM"))
    added = archive.index_row(meta_for(mid="c", date="2026-08-06"))

    rows = archive.merge_index(old, [refreshed, added])
    assert [r[archive.ID_COLUMN] for r in rows] == ["c", "b", "a"]  # no duplicate 'a'
    assert rows[2][archive.INDEX_HEADERS.index("Project")] == "HIROCOM"  # 'a' was corrected


def test_merge_index_drops_blank_and_truncated_legacy_rows():
    rows = archive.merge_index([[], ["stray"], []], [archive.index_row(meta_for())])
    assert len(rows) == 1


def test_compose_slack_links_the_folder_and_artifacts():
    text = archive.compose_slack([meta_for()], "STACX")
    assert "*Meeting archive — STACX* · 1 meeting filed" in text
    assert "<https://drive/folder|*2026-08-06* — Daily Stand-up (15:30)>" in text
    assert "<https://drive/rec|recording>" in text
    assert "Soma Pani, Raghul" in text and "SP-12" in text


# --- Otter payload normalisation -----------------------------------------
def test_as_datetime_reads_epochs_iso_and_junk():
    assert otter.as_datetime("2026-08-06T09:30:00Z").date() == dt.date(2026, 8, 6)
    epoch = dt.datetime(2026, 8, 6, 9, 30).timestamp()
    assert otter.as_datetime(epoch).date() == dt.date(2026, 8, 6)
    assert otter.as_datetime(epoch * 1000).date() == dt.date(2026, 8, 6)  # milliseconds
    assert isinstance(otter.as_datetime("not a date"), dt.datetime)  # falls back, never raises
    assert isinstance(otter.as_datetime(None), dt.datetime)


def test_rendered_transcript_round_trips_through_the_parser():
    text = otter.render_transcript([
        {"speaker": "Soma Pani", "text": "Working on SP-12.", "start": 3},
        {"speaker": "Raghul", "text": "Closed BHA-9.", "start": 3725},
    ])
    assert "Soma Pani  0:03" in text and "Raghul  1:02:05" in text
    segs = transcript.parse_transcript(text)
    assert [s["speaker"] for s in segs] == ["Soma Pani", "Raghul"]
    assert segs[1]["text"] == "Closed BHA-9."


def test_to_meeting_normalises_the_official_payload_shape():
    payload = {
        "id": "conv-9",
        "title": "Sprint Review",
        "started_at": "2026-08-06T09:30:00Z",
        "duration": 1800,
        "participants": [{"name": "Soma Pani"}, {"email": "raghul@example.com"}],
        "transcript": [{"speaker": {"name": "Soma Pani"}, "text": "SP-12 is done.", "start": 5}],
        "summary": {"text": "Reviewed the sprint."},
        "outline": ["Sprint goals", "Demos"],
        "action_items": [{"text": "Close SP-12"}],
        "audio_url": "https://otter/audio.mp3",
    }
    m = otter.to_meeting(payload, source="otter:official")
    assert m.id == "conv-9" and m.title == "Sprint Review"
    assert m.date == dt.date(2026, 8, 6) and m.duration_hms == "30:00"
    assert m.attendees == ["Soma Pani", "raghul@example.com"]
    assert "Soma Pani  0:05" in m.transcript and "SP-12 is done." in m.transcript
    assert "Reviewed the sprint." in m.summary and "- Sprint goals" in m.summary
    assert m.action_items == ["Close SP-12"] and m.audio_url == "https://otter/audio.mp3"


def test_to_meeting_normalises_the_web_payload_shape():
    payload = {
        "otid": "spe-7",
        "title": "",
        "start_time": 1786000000,
        "speakers": ["Soma"],
        "transcripts": [{"speaker_name": "Soma", "text": "Hi", "start_time": 0}],
    }
    m = otter.to_meeting(payload, source="otter:web")
    assert m.id == "spe-7"
    assert m.title == "Untitled meeting"  # empty title gets a usable placeholder
    assert m.attendees == ["Soma"] and m.audio_url is None
    assert m.duration_sec == 0 and m.duration_hms == "0:00"


def test_to_meeting_survives_an_empty_payload():
    m = otter.to_meeting({}, source="otter:official")
    assert m.id == "" and m.title == "Untitled meeting" and m.transcript == ""


# --- inbox source (no credentials) ---------------------------------------
def test_inbox_groups_files_by_stem_and_reads_the_date_from_the_name(tmp_path):
    (tmp_path / "2026-08-06 Daily Standup.txt").write_text("Soma  0:03\nHello\n", encoding="utf-8")
    (tmp_path / "2026-08-06 Daily Standup.mp3").write_bytes(b"ID3audio")
    (tmp_path / "2026-08-06 Daily Standup.summary.md").write_text("# Summary", encoding="utf-8")
    (tmp_path / "notes.docx").write_text("ignored", encoding="utf-8")  # unknown type

    source = otter.InboxSource(tmp_path)
    stubs = source.list_meetings(dt.date(2026, 8, 1), dt.date(2026, 8, 31))
    assert len(stubs) == 1  # three files, one meeting

    m = source.fetch(stubs[0])
    assert m.date == dt.date(2026, 8, 6)
    assert m.title == "Daily Standup"  # date prefix stripped
    assert "Hello" in m.transcript and m.summary == "# Summary"
    assert source.download_audio(m) == (b"ID3audio", "mp3")


def test_inbox_filters_by_window_and_ignores_missing_folders(tmp_path):
    (tmp_path / "2026-07-01 Old.txt").write_text("x", encoding="utf-8")
    source = otter.InboxSource(tmp_path)
    assert source.list_meetings(dt.date(2026, 8, 1), dt.date(2026, 8, 31)) == []
    assert otter.InboxSource(tmp_path / "nope").list_meetings(
        dt.date(2026, 1, 1), dt.date(2026, 12, 31)) == []


def test_build_source_prefers_an_explicit_inbox_and_reports_nothing_configured(tmp_path):
    assert isinstance(otter.build_source(None, tmp_path), otter.InboxSource)
    assert otter.build_source(None, None) is None
