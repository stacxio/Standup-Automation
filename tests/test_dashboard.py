"""Dashboard rendering: fact-table parsing, escaping, and the page contract."""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import build_dashboard as bd  # noqa: E402
from standup_summarizer import dashboard, scorecard as sc  # noqa: E402

NOW = dt.datetime(2026, 8, 12, 18, 30)
H = sc.DAILY_HEADERS


def row(**kw) -> list[str]:
    values = {"Date": "2026-08-12", "Developer": "Raghul", "Attendance": "Present",
              "Status": sc.SCORED, "Reason": "", "Tasks Picked": "3", "Tasks Done": "2",
              "Process": "30.0", "Delivery": "40.0", "Total": "70.0",
              "Band": "On Track", "Flags": ""}
    values.update(kw)
    return [str(values.get(name, "")) for name in H]


# --- parsing the fact table -----------------------------------------------
def test_a_scored_row_maps_onto_the_page_record():
    record = dashboard.records_from_rows([row()], H)[0]
    assert record == {
        "date": "2026-08-12", "developer": "Raghul", "attendance": "Present",
        "status": sc.SCORED, "reason": "", "picked": 3, "done": 2,
        "process": 30.0, "delivery": 40.0, "total": 70.0,
        "band": "On Track", "flags": [],
    }


def test_a_blank_total_stays_none_rather_than_becoming_zero():
    record = dashboard.records_from_rows(
        [row(Status=sc.NOT_SCORED, Total="", Process="", Delivery="",
             Reason="jira: 503")], H)[0]
    assert record["total"] is None and record["process"] is None
    assert record["status"] == sc.NOT_SCORED and record["reason"] == "jira: 503"


def test_rows_with_an_unparseable_date_are_dropped_not_guessed():
    rows = [row(), row(Date="not-a-date"), row(Date=""), [], ["stray"]]
    assert len(dashboard.records_from_rows(rows, H)) == 1


def test_rows_without_a_developer_are_dropped():
    assert dashboard.records_from_rows([row(Developer="")], H) == []


def test_flags_are_split_into_a_list():
    record = dashboard.records_from_rows([row(Flags="half_day, under_committed")], H)[0]
    assert record["flags"] == ["half_day", "under_committed"]


def test_columns_are_read_by_name_not_position():
    """A reordered fact table must not silently shift the parse."""
    headers = list(reversed(H))
    reordered = [list(reversed(row()))]
    record = dashboard.records_from_rows(reordered, headers)[0]
    assert record["developer"] == "Raghul" and record["total"] == 70.0


def test_a_fact_table_without_the_key_columns_yields_nothing():
    assert dashboard.records_from_rows([["a", "b"]], ["Foo", "Bar"]) == []


def test_records_come_back_sorted_by_date_then_developer():
    rows = [row(Date="2026-08-12", Developer="Soma"),
            row(Date="2026-08-11", Developer="Raghul"),
            row(Date="2026-08-12", Developer="Raghul")]
    got = [(r["date"], r["developer"]) for r in dashboard.records_from_rows(rows, H)]
    assert got == [("2026-08-11", "Raghul"), ("2026-08-12", "Raghul"), ("2026-08-12", "Soma")]


def test_short_rows_do_not_raise():
    assert dashboard.records_from_rows([["2026-08-12", "Raghul"]], H)[0]["total"] is None


# --- the rendered page ----------------------------------------------------
def page(records=None, **kw) -> str:
    return dashboard.render(records if records is not None else
                            dashboard.records_from_rows([row()], H),
                            generated_at=NOW, **kw)


def test_the_page_is_self_contained():
    html = page()
    # No external references of any kind — it must open from disk offline. The
    # SVG namespace URI is an identifier, not a fetch, so it is exempt.
    fetching = re.sub(r"https?://www\.w3\.org/\S*", "", html)
    assert "http://" not in fetching and "https://" not in fetching
    assert not re.search(r'<(link|img|iframe|script)\b[^>]*\bsrc\s*=', html, re.I)
    assert not re.search(r"@import|url\(\s*['\"]?https?:", html, re.I)


def test_the_page_carries_a_title_and_the_data():
    html = page()
    assert "<title>Team Scorecard</title>" in html
    assert '"developer": "Raghul"' in html or '"developer":"Raghul"' in html


def test_both_themes_are_declared_and_the_body_paints_its_own_background():
    html = page()
    assert "prefers-color-scheme: dark" in html
    assert ':root:not([data-theme="light"])' in html
    assert ':root[data-theme="dark"]' in html
    assert re.search(r"body\s*\{[^}]*background:\s*var\(--plane\)", html)


def test_wide_panels_scroll_inside_their_own_container():
    html = page()
    assert html.count("overflow-x: auto") >= 2   # heatmap and tables


def test_a_legend_is_present_for_the_two_series():
    html = page()
    assert "Process (40)" in html and "Delivery (60)" in html


def test_the_embedded_payload_is_valid_json():
    html = page()
    raw = html.split("var DATA = ", 1)[1].split(";\n", 1)[0]
    payload = json.loads(raw)
    assert payload["generated"] == "12 Aug 2026, 18:30"
    assert payload["records"][0]["developer"] == "Raghul"


def test_a_note_is_rendered_as_a_banner():
    assert "sample" in page(note="These are sample figures.").lower()


HOSTILE = '</script><img src=x onerror=alert(1)>'


def embedded(html: str) -> str:
    """The raw JSON literal as it sits inside the <script> block."""
    return html.split("var DATA = ", 1)[1].split(";\n", 1)[0]


def test_a_hostile_developer_name_cannot_break_out_of_the_script_block():
    """Slack display names are user-controlled and land inside <script>."""
    html = page(dashboard.records_from_rows([row(Developer=HOSTILE)], H))
    payload = embedded(html)
    # No angle bracket from data survives, so no tag can form and close the block.
    assert "<" not in payload and ">" not in payload
    # ...and the value is unchanged once the browser parses it.
    assert json.loads(payload)["records"][0]["developer"] == HOSTILE


def test_a_hostile_note_cannot_break_out_either():
    payload = embedded(page(note=HOSTILE))
    assert "<" not in payload and ">" not in payload
    assert json.loads(payload)["note"] == HOSTILE


def test_the_title_is_escaped():
    html = page(title="<script>alert(1)</script>")
    assert "<title>&lt;script&gt;alert(1)&lt;/script&gt;</title>" in html
    assert "<script>alert(1)" not in html


def test_an_empty_fact_table_still_renders():
    html = page([])
    assert "<title>" in html and '"records": []' in html.replace('"records":[]', '"records": []')


# --- the demo dataset -----------------------------------------------------
def test_demo_data_is_deterministic_and_uses_placeholder_names():
    first, second = bd.demo_records(days=20), bd.demo_records(days=20)
    assert first == second                                   # same seed, same page
    names = {r["developer"] for r in first}
    assert names and all(n.startswith("Developer ") for n in names)


def test_demo_data_skips_weekends_and_includes_the_edge_cases():
    records = bd.demo_records(days=30)
    assert all(dt.date.fromisoformat(r["date"]).weekday() < 5 for r in records)
    statuses = {r["status"] for r in records}
    assert statuses == {sc.SCORED, sc.NOT_SCORED}
    assert any(r["attendance"] == "Absent" and r["total"] == 0.0 for r in records)
    assert all(r["total"] is None for r in records if r["status"] == sc.NOT_SCORED)


def test_demo_records_render():
    html = dashboard.render(bd.demo_records(days=10), generated_at=NOW, note=bd.DEMO_NOTE)
    assert "Developer A" in html and "sample data" in html.lower()
