"""Daily performance scoring rules — pure, offline (docs/SCORING.md)."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from standup_summarizer import scorecard as sc  # noqa: E402

TODAY = dt.date(2026, 8, 12)
YESTERDAY = dt.date(2026, 8, 11)
NOON = dt.datetime(2026, 8, 12, 18, 0, 4)

W = sc.Weights()
T = sc.Thresholds()


def comment(body: str, *, day: dt.date = TODAY, theirs: bool = True) -> sc.CommentFacts:
    return sc.CommentFacts(body=body, created=day, authored_by_developer=theirs)


def task(key: str = "SP-12", *, desc: str = "x" * 60, status: str = "In Progress",
         category: str = "In Progress", kind: str = "Story",
         commit: bool = False, comments=()) -> sc.TaskFacts:
    return sc.TaskFacts(key=key, description=desc, status=status, status_category=category,
                        issue_type=kind, has_linked_commit=commit,
                        comments=tuple(comments), url=f"https://jira/browse/{key}")


def day(**kw) -> sc.DayFacts:
    base = dict(developer="Raghul", date=TODAY, attendance="Present", checked_in=True,
                picked_tasks=(), tasks=(), median_picked=None)
    base.update(kw)
    return sc.DayFacts(**base)


def score(facts: sc.DayFacts, thresholds: sc.Thresholds = T) -> dict:
    return sc.score_day(facts, W, thresholds, computed_at=NOON)


# --- configuration --------------------------------------------------------
def test_weights_must_sum_to_one_hundred():
    sc.Weights().validate()  # the default rubric
    with pytest.raises(ValueError):
        sc.Weights(done=55.0).validate()
    with pytest.raises(ValueError):
        sc.Weights(done=65.0).validate()


def test_scoring_refuses_to_run_on_bad_weights():
    with pytest.raises(ValueError):
        sc.score_day(day(), sc.Weights(checkin=99.0), T, computed_at=NOON)


# --- the canonical case (SCORING.md §11.1) --------------------------------
def canonical() -> sc.DayFacts:
    return day(
        picked_tasks=("SP-12", "SP-15", "HIR-72"),
        median_picked=3.0,
        tasks=(
            task("SP-12", desc="x" * 412, status="Done", category="Done", commit=True,
                 comments=[comment("Implemented the endpoint and merged a1b2c3d into main.")]),
            task("SP-15", desc="x" * 200, status="In Review", category="In Progress",
                 comments=[comment("PR is up: https://github.com/o/r/commit/9f8e7d6")]),
            task("HIR-72", desc="fix", kind="Bug"),
        ),
    )


def test_canonical_day_scores():
    record = score(canonical())
    assert record["points"] == {"checkin": 10.0, "picked": 5.0, "description": 6.7,
                                "commit": 3.3, "comment": 6.7, "done": 45.0}
    assert record["process"] == 31.7
    assert record["delivery"] == 45.0    # (1.0 + 1.0 + 0.25) / 3 x 60
    assert record["total"] == 76.7
    assert record["band"] == "On Track"
    assert record["tasks_done"] == 2 and record["tasks_picked"] == 3
    assert record["tasks_credit"] == 2.25
    assert record["status"] == sc.SCORED and record["flags"] == []


def test_rounded_components_would_not_add_up():
    """The guard behind §3.1 — components must sum before they are rounded."""
    facts = day(picked_tasks=("A-1", "A-2", "A-3"), median_picked=3.0, tasks=(
        task("A-1", desc="x" * 60, commit=True, comments=[comment("Did the work on this today.")]),
        task("A-2", desc="x" * 60, comments=[comment("Also did work here today.")]),
        task("A-3", desc="no"),
    ))
    record = score(facts)
    # 6.7 + 1.7 + 6.7 = 15.1 rounded, but 20/3 + 5/3 + 20/3 = 15.0 exactly.
    naive = sum(record["points"][k] for k in ("description", "commit", "comment"))
    assert naive == 15.1
    assert record["process"] == 30.0


def test_evidence_records_every_task_and_why_it_failed():
    tasks = score(canonical())["evidence"]["tasks"]
    assert [t["key"] for t in tasks] == ["SP-12", "SP-15", "HIR-72"]  # picked order
    assert tasks[1]["done"] == {"passed": True, "credit": 1.0, "rule": "review_with_commit"}
    assert tasks[2]["done"] == {"passed": False, "credit": 0.25, "rule": "in_progress"}
    assert tasks[2]["description"] == {"passed": False, "chars": 3, "source": "issue"}
    assert tasks[2]["comment"]["reason"] == "no comment by them on the day"
    assert tasks[0]["url"].endswith("SP-12")


def test_scoring_is_deterministic():
    assert score(canonical()) == score(canonical())


# --- decision order (§4) --------------------------------------------------
def test_unusable_data_is_not_scored():
    record = score(day(data_ok=False, data_error="jira: 503"))
    assert record["status"] == sc.NOT_SCORED
    assert record["total"] is None and record["reason"] == "jira: 503"


def test_partial_jira_facts_are_not_scored_rather_than_understated():
    record = score(day(picked_tasks=("SP-12", "SP-15"), tasks=(task("SP-12"),)))
    assert record["status"] == sc.NOT_SCORED
    assert "SP-15" in record["reason"]


def test_missing_facts_outrank_absence():
    # Rule 2 fires before rule 4, so a data failure never masquerades as a zero.
    record = score(day(attendance="Absent", picked_tasks=("SP-12",), tasks=()))
    assert record["status"] == sc.NOT_SCORED


@pytest.mark.parametrize("attendance", ["Leave", "Weekend", "Holiday", "leave"])
def test_non_working_days_are_excluded_not_zeroed(attendance):
    record = score(day(attendance=attendance))
    assert record["status"] == sc.NOT_SCORED and record["total"] is None


def test_absence_is_a_real_zero():
    record = score(day(attendance="Absent", checked_in=False))
    assert record["status"] == sc.SCORED
    assert record["total"] == 0.0 and record["band"] == "At Risk"
    assert record["points"] == {"checkin": 0.0, "picked": 0.0, "description": 0.0,
                                "commit": 0.0, "comment": 0.0, "done": 0.0}


# --- per-day checks -------------------------------------------------------
def test_check_in_is_the_only_thing_a_taskless_day_can_earn():
    record = score(day(checked_in=True))
    assert record["total"] == 10.0 and record["flags"] == ["no_tasks"]
    assert record["points"]["picked"] == 0.0


def test_present_without_a_check_in_scores_zero_for_it():
    record = score(day(checked_in=False, picked_tasks=("SP-12",), tasks=(task(),)))
    assert record["points"]["checkin"] == 0.0
    assert record["evidence"]["checkin"] == {"passed": False}


def test_half_day_is_flagged():
    assert "half_day" in score(day(attendance="Half Day"))["flags"]


# --- description (§4.3) ---------------------------------------------------
@pytest.mark.parametrize("chars,passed", [(29, False), (30, True), (31, True), (0, False)])
def test_description_length_boundary(chars, passed):
    record = score(day(picked_tasks=("SP-12",), tasks=(task(desc="x" * chars),)))
    assert record["evidence"]["tasks"][0]["description"]["passed"] is passed


def test_whitespace_only_description_does_not_count():
    record = score(day(picked_tasks=("SP-12",), tasks=(task(desc="   \n  " * 20),)))
    assert record["evidence"]["tasks"][0]["description"]["chars"] == 0


# --- commit (§4.4) --------------------------------------------------------
def test_the_dev_panel_is_the_preferred_commit_source():
    linked = score(day(picked_tasks=("SP-12",), tasks=(task(commit=True),)))
    assert linked["points"]["commit"] == 5.0
    assert linked["evidence"]["tasks"][0]["commit"]["source"] == "dev_panel"


def test_a_commit_id_in_a_comment_counts_when_no_scm_is_linked():
    """This Jira has no SCM integration, so the panel is permanently empty."""
    typed = score(day(picked_tasks=("SP-12",),
                      tasks=(task(comments=[comment("Pushed a1b2c3d to the branch today.")]),)))
    assert typed["points"]["commit"] == 5.0
    assert typed["evidence"]["tasks"][0]["commit"]["source"] == "comment"


def test_the_comment_fallback_can_be_turned_off():
    strict = sc.Thresholds(commit_in_comment_counts=False)
    record = score(day(picked_tasks=("SP-12",),
                       tasks=(task(comments=[comment("Pushed a1b2c3d today.")]),)), strict)
    assert record["points"]["commit"] == 0.0


def test_a_comment_without_a_commit_id_still_earns_nothing_for_commit():
    record = score(day(picked_tasks=("SP-12",),
                       tasks=(task(comments=[comment("Still working through the edge cases.")]),)))
    assert record["points"]["commit"] == 0.0


# --- sub-task descriptions ------------------------------------------------
def test_a_subtask_inherits_its_parents_description():
    """Sub-tasks routinely carry no description — the context is on the parent."""
    sub = sc.TaskFacts(key="WS-230", description="", parent_description="x" * 400,
                       status="Review", status_category="In Progress")
    record = score(day(picked_tasks=("WS-230",), tasks=(sub,)))
    assert record["points"]["description"] == 10.0
    assert record["evidence"]["tasks"][0]["description"]["source"] == "parent"


def test_the_issues_own_description_wins_when_it_has_one():
    both = sc.TaskFacts(key="WS-230", description="x" * 400, parent_description="x" * 999)
    assert sc.description_check(both, T) == {"passed": True, "chars": 400, "source": "issue"}


def test_a_thin_parent_does_not_rescue_a_thin_subtask():
    thin = sc.TaskFacts(key="WS-230", description="", parent_description="also short")
    assert sc.description_check(thin, T)["passed"] is False


def test_the_parent_fallback_can_be_turned_off():
    sub = sc.TaskFacts(key="WS-230", description="", parent_description="x" * 400)
    assert sc.description_check(sub, sc.Thresholds(parent_description_fallback=False))["passed"] is False


# --- screenshot comments --------------------------------------------------
def media_comment(names=("Screenshot 2026-08-11.png",), body="", **kw):
    return sc.CommentFacts(body=body, created=TODAY, authored_by_developer=True,
                           media=tuple(names), **kw)


def test_a_screenshot_only_comment_counts_as_an_update():
    """It flattens to zero characters but is a real ticket update."""
    record = score(day(picked_tasks=("BHA-106",), tasks=(task("BHA-106",
                                                              comments=[media_comment()]),)))
    assert record["points"]["comment"] == 10.0
    evidence = record["evidence"]["tasks"][0]["comment"]
    assert evidence["source"] == "media" and evidence["chars"] == 0


def test_a_text_comment_reports_its_source_as_text():
    record = score(day(picked_tasks=("SP-12",),
                       tasks=(task(comments=[comment("Finished the validation layer.")]),)))
    assert record["evidence"]["tasks"][0]["comment"]["source"] == "text"


def test_the_media_rule_can_be_turned_off():
    strict = sc.Thresholds(media_counts_as_comment=False)
    record = score(day(picked_tasks=("BHA-106",),
                       tasks=(task("BHA-106", comments=[media_comment()]),)), strict)
    assert record["points"]["comment"] == 0.0


def test_someone_elses_screenshot_does_not_count():
    theirs = sc.CommentFacts(body="", created=TODAY, authored_by_developer=False,
                             media=("shot.png",))
    record = score(day(picked_tasks=("SP-12",), tasks=(task(comments=[theirs]),)))
    assert record["points"]["comment"] == 0.0


# --- the delivery credit ladder -------------------------------------------
@pytest.mark.parametrize("status,category,comments,credit,rule", [
    ("Done", "Done", (), 1.0, "done"),
    ("Review", "In Progress", ("commit a1b2c3d",), 1.0, "review_with_commit"),
    ("Review", "In Progress", ("Ready for review, no blockers.",), 0.5, "review"),
    ("In Review", "In Progress", (), 0.5, "review"),
    ("In Progress", "In Progress", (), 0.25, "in_progress"),
    ("To Do", "To Do", (), 0.0, "todo"),
    ("Backlog", "To Do", ("commit a1b2c3d",), 0.0, "todo"),
])
def test_the_credit_ladder(status, category, comments, credit, rule):
    t = task(status=status, category=category,
             comments=[comment(body) for body in comments])
    assert sc.task_credit(t, T) == (credit, rule)


def test_review_now_earns_half_instead_of_nothing():
    """The change that matters most: work sitting in review is not work not done."""
    facts = day(picked_tasks=("WS-230", "WS-231"), median_picked=2.0, tasks=(
        task("WS-230", status="Review", category="In Progress"),
        task("WS-231", status="Review", category="In Progress"),
    ))
    assert score(facts)["delivery"] == 30.0      # was 0.0 under the binary rule


def test_the_ladder_is_configurable():
    harsh = sc.Thresholds(credit_review=0.0, credit_in_progress=0.0)
    t = task(status="Review", category="In Progress")
    assert sc.task_credit(t, harsh) == (0.0, "review")


# --- comment (§4.5) -------------------------------------------------------
def one_comment(*comments) -> dict:
    return score(day(picked_tasks=("SP-12",), tasks=(task(comments=comments),)))


def test_a_good_comment_scores():
    record = one_comment(comment("Finished the validation layer and pushed it."))
    assert record["points"]["comment"] == 10.0


def test_someone_elses_comment_does_not_score_for_them():
    assert one_comment(comment("Reviewed and looks good to me.",
                               theirs=False))["points"]["comment"] == 0.0


def test_yesterdays_comment_does_not_score_again_today():
    assert one_comment(comment("Finished the validation layer.",
                               day=YESTERDAY))["points"]["comment"] == 0.0


def test_a_short_comment_does_not_score():
    record = one_comment(comment("ok"))
    assert record["points"]["comment"] == 0.0
    assert record["evidence"]["tasks"][0]["comment"]["reason"] == "too short"


def test_a_repeated_comment_does_not_score():
    body = "Still working on this, no blockers."
    record = one_comment(comment(body, day=YESTERDAY), comment(body))
    assert record["points"]["comment"] == 0.0
    assert record["evidence"]["tasks"][0]["comment"]["reason"] == "duplicate of an earlier comment"


def test_reformatting_a_repeat_does_not_evade_the_check():
    record = one_comment(comment("Still working on this, no blockers.", day=YESTERDAY),
                         comment("  STILL   working on this,  no blockers.  "))
    assert record["points"]["comment"] == 0.0


def test_a_genuinely_new_comment_scores_despite_an_earlier_one():
    record = one_comment(comment("Started on the migration script.", day=YESTERDAY),
                         comment("Migration script finished and tested."))
    assert record["points"]["comment"] == 10.0


# --- commit references in text (§4.6) -------------------------------------
@pytest.mark.parametrize("text", [
    "merged a1b2c3d",
    "see https://github.com/org/repo/commit/9f8e7d6c5b4a3928170",
    "org/repo/commits/9f8e7d6",
    "sha 9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c",
])
def test_commit_reference_is_recognised(text):
    assert sc.has_commit_reference(text) is True


@pytest.mark.parametrize("text", [
    "deadbeef",                 # hex letters, no digit — an English-looking token
    "the facade is done",       # too short anyway
    "accede to the request",
    "SP-1234567 is next",       # the numeric half of a Jira key
    "xdeadbeef1x",              # inside a longer word
    "1234567",                  # digits only — a ticket or build number
    "",
])
def test_commit_reference_false_positives_are_rejected(text):
    assert sc.has_commit_reference(text) is False


# --- done (§4.6) ----------------------------------------------------------
def test_done_by_status_category():
    passed, rule = sc.task_is_done(task(status="Done", category="Done"), T)
    assert (passed, rule) == (True, "done")


def test_review_with_a_commit_in_a_comment_counts_as_done():
    t = task(status="In Review", comments=[comment("PR raised, commit a1b2c3d.")])
    assert sc.task_is_done(t, T) == (True, "review_with_commit")


def test_review_without_a_commit_does_not_count():
    t = task(status="In Review", comments=[comment("PR raised for review.")])
    assert sc.task_is_done(t, T) == (False, "")


def test_a_commit_alone_does_not_finish_an_in_progress_task():
    t = task(status="In Progress", comments=[comment("Pushed a1b2c3d.")])
    assert sc.task_is_done(t, T) == (False, "")


def test_status_matching_tolerates_hand_edited_case_and_spacing():
    t = task(status="  in   REVIEW ", comments=[comment("commit a1b2c3d")])
    assert sc.task_is_done(t, T)[0] is True


# --- volume factor (§5.1) -------------------------------------------------
@pytest.mark.parametrize("n,median,expected", [
    (3, 3.0, 1.0),      # at baseline
    (5, 3.0, 1.0),      # above baseline is never rewarded
    (1, 3.0, 1 / 3),    # under-committed
    (3, None, 1.0),     # not enough history yet
    (1, 1.0, 1.0),      # median below min_median_tasks — check stays dormant
    (0, 3.0, 0.0),
])
def test_volume_factor(n, median, expected):
    assert sc.volume_factor(n, median, T.min_median_tasks) == pytest.approx(expected)


def test_under_commitment_scales_delivery_down():
    facts = day(picked_tasks=("SP-12",), median_picked=3.0,
                tasks=(task(status="Done", category="Done", desc="x" * 60, commit=True,
                            comments=[comment("Completed and merged the change.")]),))
    record = score(facts)
    assert record["process"] == 40.0            # every process check passed
    assert record["delivery"] == 20.0           # 60 x 1.0 x 0.33
    assert record["total"] == 60.0              # would be 100 without §5.1
    assert "under_committed" in record["flags"]


# --- unplanned work (§7.1) ------------------------------------------------
def test_incident_work_is_scored_on_process_alone():
    facts = day(picked_tasks=("SUP-4",),
                tasks=(task("SUP-4", kind="Incident", desc="fix", commit=True,
                            comments=[comment("Restored the service and closed it out.")]),))
    record = score(facts)
    assert "unplanned_work" in record["flags"]
    assert record["process"] == 30.0            # description failed
    assert record["delivery"] == 0.0
    assert record["total"] == 75.0              # 30 rescaled to 100


def test_incident_projects_are_configurable():
    thresholds = sc.Thresholds(incident_projects=frozenset({"OPS"}))
    record = score(day(picked_tasks=("OPS-9",), tasks=(task("OPS-9"),)), thresholds)
    assert "unplanned_work" in record["flags"]


# --- bands ----------------------------------------------------------------
@pytest.mark.parametrize("total,band", [
    (100, "Excellent"), (85, "Excellent"), (84.9, "On Track"), (70, "On Track"),
    (69.9, "Needs Attention"), (50, "Needs Attention"), (49.9, "At Risk"), (0, "At Risk"),
])
def test_bands(total, band):
    assert sc.band_of(total) == band


# --- averaging ------------------------------------------------------------
def record_stub(total, attendance="Present", status=sc.SCORED, date="2026-08-12"):
    return {"developer": "Raghul", "date": date, "attendance": attendance,
            "status": status, "total": total}


def test_not_scored_days_are_excluded_from_the_average():
    records = [record_stub(80), record_stub(None, "Leave", sc.NOT_SCORED), record_stub(60)]
    assert sc.period_average(records) == 70.0


def test_half_days_count_half():
    assert sc.period_average([record_stub(80), record_stub(60, "Half Day")]) == 73.3


def test_an_entirely_unscored_period_has_no_average():
    assert sc.period_average([record_stub(None, "Leave", sc.NOT_SCORED)]) is None
    assert sc.period_average([]) is None


def test_absence_does_drag_the_average_down():
    assert sc.period_average([record_stub(80), record_stub(0.0, "Absent")]) == 40.0


# --- sheet surfaces -------------------------------------------------------
def test_daily_row_matches_the_header_layout():
    row = sc.daily_row(score(canonical()))
    assert len(row) == len(sc.DAILY_HEADERS)
    assert row[sc.DATE_COLUMN] == "2026-08-12"
    assert row[sc.DEVELOPER_COLUMN] == "Raghul"
    assert row[sc.DAILY_HEADERS.index("Total")] == "76.7"
    assert row[sc.DAILY_HEADERS.index("Task Credit")] == "2.25"
    assert row[sc.DAILY_HEADERS.index("Picked Tasks")] == "SP-12, SP-15, HIR-72"
    assert "SP-12" in row[sc.DAILY_HEADERS.index("Evidence")]


def test_a_not_scored_row_leaves_the_total_blank():
    row = sc.daily_row(score(day(data_ok=False, data_error="jira: 503")))
    assert row[sc.DAILY_HEADERS.index("Total")] == ""
    assert row[sc.DAILY_HEADERS.index("Status")] == sc.NOT_SCORED


def test_merge_daily_upserts_by_date_and_developer():
    def row(date, name, total):
        return sc.daily_row({**record_stub(total, date=date), "developer": name,
                             "points": {}, "picked_tasks": [], "flags": [], "evidence": {}})

    existing = [row("2026-08-10", "Raghul", 50.0), row("2026-08-11", "Soma", 60.0)]
    rows = sc.merge_daily(existing, [row("2026-08-10", "Raghul", 90.0),
                                     row("2026-08-12", "Soma", 70.0)])
    totals = sc.DAILY_HEADERS.index("Total")
    assert len(rows) == 3                                   # the re-run corrected, not appended
    assert rows[0][sc.DATE_COLUMN] == "2026-08-12"          # newest first
    assert rows[-1][totals] == "90.0"                       # 10 Aug was rewritten


def test_merge_daily_drops_blank_and_truncated_legacy_rows():
    rows = sc.merge_daily([[], ["stray"], []], [sc.daily_row(score(canonical()))])
    assert len(rows) == 1


def test_month_matrix_blanks_unscored_days():
    records = [score(canonical()),
               score(day(date=dt.date(2026, 8, 11), attendance="Leave"))]
    header, rows = sc.month_matrix(records, 2026, 8, ["Raghul", "Soma"])
    assert header[0] == "Developer" and header[-1] == "Average"
    assert len(header) == 1 + 31 + 1                        # August
    assert rows[0][12] == "76.7"                            # 12 Aug is column 12
    assert rows[0][11] == ""                                # 11 Aug was Leave
    assert rows[0][-1] == "76.7"                            # Leave excluded
    assert rows[1] == ["Soma"] + [""] * 31 + [""]           # no records at all


def test_month_matrix_handles_december():
    header, _ = sc.month_matrix([], 2026, 12, ["Raghul"])
    assert len(header) == 1 + 31 + 1


# --- slack surfaces -------------------------------------------------------
def test_dm_states_the_score_and_the_breakdown():
    text = sc.compose_slack_dm(score(canonical()))
    assert "*76.7/100*" in text and "On Track" in text
    assert "Process 31.7/40" in text and "Delivery 45.0/60" in text
    assert "Jira description: 6.7/10" in text
    assert "<https://jira/browse/SP-12|SP-12>" in text
    # Partial delivery must be visible per task, or a 0.25 looks like a 0.
    assert "delivery 0.25" in text and "2.25 credit" in text


def test_dm_explains_a_not_scored_day_rather_than_showing_zero():
    text = sc.compose_slack_dm(score(day(data_ok=False, data_error="jira: 503")))
    assert "Not scored: jira: 503" in text
    assert "excluded from your average" in text
    assert "/100" not in text


def roster_records() -> list[dict]:
    return [
        score(canonical()),                                              # Raghul 70.0
        score(day(developer="Soma", picked_tasks=("SP-20",), median_picked=1.0,
                  tasks=(task("SP-20", desc="x" * 90, status="Done", category="Done",
                              commit=True,
                              comments=[comment("Shipped and merged the change.")]),))),
        score(day(developer="Nadia", attendance="Absent", checked_in=False)),
        score(day(developer="Arjun", data_ok=False, data_error="jira: 503")),
        score(day(developer="Priya", attendance="Half Day")),
    ]


def test_the_daily_post_lists_every_developer():
    text = sc.compose_slack_roster(roster_records())
    for name in ("Raghul", "Soma", "Nadia", "Arjun", "Priya"):
        assert name in text
    assert "*Daily Scorecard — 2026-08-12*" in text
    assert "```" in text                                  # fixed-width, so columns align


def test_the_daily_post_shows_each_persons_split_not_just_a_total():
    text = sc.compose_slack_roster(roster_records())
    assert "Total" in text and "Process" in text and "Delivery" in text
    assert "76.7" in text and "31.7" in text and "45.0" in text


def test_absence_and_data_failure_read_differently_in_the_post():
    text = sc.compose_slack_roster(roster_records())
    assert "Absent" in text
    assert "Not scored — jira: 503" in text
    assert "half day" in text.lower()


def test_a_not_scored_developer_shows_dashes_not_zero():
    text = sc.compose_slack_roster([score(day(developer="Arjun", data_ok=False,
                                              data_error="jira: 503"))])
    assert "0.0" not in text.split("```")[1]


def test_roster_order_is_the_default_and_score_order_is_opt_in():
    records = roster_records()
    body = sc.compose_slack_roster(records).split("```")[1]
    assert body.index("Raghul") < body.index("Soma") < body.index("Nadia")

    ranked = sc.compose_slack_roster(records, order="score").split("```")[1]
    assert ranked.index("Soma") < ranked.index("Raghul")     # 100.0 before 70.0
    assert ranked.index("Arjun") > ranked.index("Nadia")     # not-scored sinks last


def test_the_post_carries_the_rubric_so_a_score_is_self_explaining():
    text = sc.compose_slack_roster(roster_records())
    assert "check-in 10" in text and "description 10" in text and "Delivery 60" in text


def test_long_names_do_not_break_the_column_alignment():
    records = [score(day(developer="Bartholomew Fitzgerald")),
               score(day(developer="Al"))]
    lines = [ln for ln in sc.compose_slack_roster(records).split("```")[1].splitlines() if ln]
    header, rows = lines[0], lines[1:]
    end = header.index("Total") + len("Total")
    # Every row's total ends in the same column the header's does.
    for line in rows:
        assert line[end - 5:end].strip() in {"10.0", "—"}
        assert line[end] == " "


def test_an_empty_roster_says_so():
    assert "nothing to report" in sc.compose_slack_roster([])


def test_team_post_aggregates_without_ranking():
    records = [score(canonical()),
               score(day(developer="Soma", attendance="Absent", checked_in=False)),
               score(day(developer="Nadia", data_ok=False, data_error="jira: 503"))]
    text = sc.compose_slack_team(records)
    assert "Team average" in text and "Absent: 1" in text
    assert "Not scored: 1" in text
    assert "Soma" not in text and "Raghul" not in text     # no per-person ranking


# --- commit ids: exact values, deduplicated -------------------------------
def test_commit_ids_are_returned_verbatim_in_document_order():
    assert sc.commit_ids_in_text("Latest commits: agb-admin: 2934bb5 agb: 11f33ea") == \
        ["2934bb5", "11f33ea"]


def test_a_short_sha_abbreviating_a_listed_full_sha_is_not_counted_twice():
    """Developers write both forms of one commit in the same comment."""
    full = "b4eda6544adcba92984f2c101ee6fe6db81793b5"
    ids = sc.commit_ids_in_text(f"merged {full} (short: b4eda65) and c8b568a")
    assert ids == [full, "c8b568a"]          # the full form wins, the short goes


def test_two_genuinely_different_shas_both_survive():
    assert sc.commit_ids_in_text("2934bb5 and 11f33ea") == ["2934bb5", "11f33ea"]


def test_a_short_sha_alone_is_kept():
    assert sc.commit_ids_in_text("merged b4eda65") == ["b4eda65"]


@pytest.mark.parametrize("body,expected", [
    ("Latest commit: https://github.com/o/r/commit/2934bb5", ["2934bb5"]),
    ("commit id - https://short.link/abc", ["https://short.link/abc"]),
    ("Commit ID: https://short.link/abc.", ["https://short.link/abc"]),
    ("We commit to shipping this: https://docs/plan", []),
    ("Latest commit: tomorrow", []),
])
def test_labelled_commit_urls(body, expected):
    assert sc.commit_ids_in_text(body) == expected


def test_a_labelled_commit_path_url_is_counted_once_not_twice():
    assert sc.commit_ids_in_text(
        "Latest commit: https://github.com/o/r/commit/2934bb5") == ["2934bb5"]
