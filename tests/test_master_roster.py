"""The employee Master roster and what a mid-month joiner does to attendance.

Covers the three joiners added on 03-Aug-2026 (Gokul, Mallesh, Madhan):
the Master seed's shape, that build_salary_slips still reads it positionally
now that a fifth column exists, and that days before someone joined are blank
rather than counted as an absence against them.
"""

from __future__ import annotations

import datetime as dt

import build_attendance as ba
import build_salary_slips as bss

JOINERS = ("Gokul", "Mallesh", "Madhan")
JOIN_DATE = "03-Aug-2026"


# --------------------------------------------------------------------------
# Master seed shape
# --------------------------------------------------------------------------
def test_seed_rows_match_the_headers():
    """A ragged seed would silently misalign every column in the sheet."""
    for row in ba.MASTER_SEED:
        assert len(row) == len(ba.MASTER_HEADERS), row


def test_join_date_is_the_last_column():
    """read_master() reads columns 0-3 positionally, so the new column must
    sit after Gross Salary or payslips read the wrong fields."""
    assert ba.MASTER_HEADERS.index("Date of Joining") == len(ba.MASTER_HEADERS) - 1
    assert ba.MASTER_HEADERS[:4] == ["Name", "Employee ID", "Designation", "Gross Salary"]


def test_joiners_are_seeded_with_their_join_date():
    seeded = {row[0]: row for row in ba.MASTER_SEED}
    for name in JOINERS:
        assert name in seeded, f"{name} missing from MASTER_SEED"
        assert seeded[name][-1] == JOIN_DATE


# --------------------------------------------------------------------------
# build_salary_slips.read_master against the widened tab
# --------------------------------------------------------------------------
class _FakeSheet:
    """The slice of a gspread Spreadsheet that read_master() touches."""

    def __init__(self, grid: list[list[str]]) -> None:
        self._grid = grid

    def worksheet(self, title: str):
        assert title == "Master"
        return self

    def get_all_values(self) -> list[list[str]]:
        return self._grid


LIVE_MASTER = [
    ["Name", "Employee ID", "Designation", "Gross Salary", "Date of Joining"],
    ["GN", "1234", "Engineer", "50,000", ""],
    ["Soma", "9999", "Engineer", "50,000", ""],
    ["Gokul", "", "", "", JOIN_DATE],
]


def test_read_master_ignores_the_extra_column():
    rows = bss.read_master(_FakeSheet(LIVE_MASTER))
    assert [r["short"] for r in rows] == ["GN", "Soma", "Gokul"]
    gn = rows[0]
    assert (gn["emp_id"], gn["designation"], gn["gross"]) == ("1234", "Engineer", 50000.0)


def test_a_joiner_without_salary_details_reads_as_zero_not_a_crash():
    """Employee ID / Designation / Gross Salary are left blank until HR fills
    them in; that must degrade to a zero payslip, never a ValueError."""
    gokul = bss.read_master(_FakeSheet(LIVE_MASTER))[-1]
    assert gokul["gross"] == 0.0
    assert gokul["emp_id"] == "" and gokul["designation"] == ""


# --------------------------------------------------------------------------
# A mid-month joiner must not accrue absences before their start date
# --------------------------------------------------------------------------
def _roll_calls(name: str, days: list[dt.date], status: str = "Present"):
    return {d: {name: status} for d in days}


def test_days_before_a_joiner_started_are_blank_not_absent():
    """Someone who started on Wed 5 Aug has no roll-call on Mon 3 / Tue 4.
    Those weekdays must stay blank, or the joiner is marked absent for time
    before they were employed."""
    started = dt.date(2026, 8, 5)
    worked = [dt.date(2026, 8, d) for d in (5, 6, 7)]
    roll_calls = _roll_calls("Gokul", worked)

    for before in (dt.date(2026, 8, 3), dt.date(2026, 8, 4)):
        assert before.weekday() < 5, "guard: these must be weekdays to be meaningful"
        assert ba.cell_value("Gokul", before, roll_calls, {}) == ""
    assert ba.cell_value("Gokul", started, roll_calls, {}) == "Present"


def test_a_joiner_accrues_no_absence_for_the_month_they_joined():
    worked = [dt.date(2026, 8, d) for d in (5, 6, 7)]
    _, rows = ba.build_summary(["Gokul"], _roll_calls("Gokul", worked), {}, 2026)
    aug = rows[0][8]          # Name, Jan..Dec -> August is index 8
    assert aug == 0
    assert rows[0][-1] == 0   # year total


def test_the_absence_count_still_works_so_the_test_above_is_not_vacuous():
    absent = {dt.date(2026, 8, 5): {"Gokul": "Absent"}}
    _, rows = ba.build_summary(["Gokul"], absent, {}, 2026)
    assert rows[0][8] == 1


def test_the_three_joined_on_a_monday_so_august_has_no_pre_join_weekday():
    """03-Aug-2026 is a Monday and 1-2 Aug are the weekend, so August holds no
    working day before they joined — their month is complete from day one."""
    join = dt.date(2026, 8, 3)
    assert join.weekday() == 0
    assert all(dt.date(2026, 8, d).weekday() >= 5 for d in (1, 2))


def test_a_joiner_row_renders_in_the_month_matrix_once_roll_calls_name_them():
    """Month rows come from roll-calls, not from Master — this is what makes a
    joiner appear in the grid at all."""
    worked = [dt.date(2026, 8, 3)]
    header, rows, _ = ba.build_month(
        list(JOINERS), _roll_calls("Gokul", worked), {}, {}, 2026, 8
    )
    assert header[0] == "Name" and len(header) == 32          # Name + 31 days
    assert [r[0] for r in rows] == list(JOINERS)
    assert rows[0][3] == "Present"                            # Gokul, 08-03-26
    assert rows[1][3] == "" and rows[2][3] == ""              # no roll-call yet
