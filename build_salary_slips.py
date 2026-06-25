"""Generate monthly salary slips (PDF) from the attendance Summary tab.

For a pay-period month it reads each person's absence from the Summary tab of the
attendance Google Sheet, computes Working Days (weekdays in the month), Present
Days (= Working - Absent), and renders a salary slip PDF (stacx24 logo + table)
per person into Salary_Slips/.

Pay period defaults to the current month; override with PAY_PERIOD=YYYY-MM.

Run:  .venv/Scripts/python.exe build_salary_slips.py
"""

from __future__ import annotations

import calendar
import datetime as dt
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

ROOT = Path(__file__).parent
SPREADSHEET_ID = os.environ.get("ATTENDANCE_SPREADSHEET_ID", "1W3H2uMFG__KTSXDw0trJi71M1RahQS65_bao8EC4sOI")
KEY_PATH = os.environ.get("GOOGLE_SA_KEY_PATH", "credentials/google_credentials.json")
BANNER = ROOT / "assets" / "stacx_banner.jpg"
OUT_DIR = ROOT / "Salary_Slips"

# Summary short names -> full names for the slip.
NAME_MAP = {"GN": "G N", "Soma": "Soma Pani", "Raghul": "Raghul", "Sahil": "Sahil Thakur"}
MONTH_ABBR = [calendar.month_abbr[m] for m in range(1, 13)]


def read_summary() -> tuple[list[str], list[list[str]]]:
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(
        KEY_PATH, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    ws = gspread.authorize(creds).open_by_key(SPREADSHEET_ID).worksheet("Summary")
    vals = ws.get_all_values()
    hdr_idx = next(i for i, r in enumerate(vals) if r and r[0] == "Name")
    return vals[hdr_idx], [r for r in vals[hdr_idx + 1:] if r and r[0]]


def working_days(year: int, month: int) -> int:
    """Count weekdays (Mon-Fri) in the month."""
    ndays = calendar.monthrange(year, month)[1]
    return sum(1 for d in range(1, ndays + 1) if dt.date(year, month, d).weekday() < 5)


def make_slip(name, pay_period, work, present, absent, out_path: Path) -> None:
    styles = getSampleStyleSheet()
    title = ParagraphStyle("slip_title", parent=styles["Title"], fontSize=20, spaceAfter=4)

    doc = SimpleDocTemplate(
        str(out_path), pagesize=A4,
        topMargin=16 * mm, bottomMargin=20 * mm, leftMargin=20 * mm, rightMargin=20 * mm,
    )
    banner = Image(str(BANNER), width=170 * mm, height=170 * mm * 132 / 430)
    banner.hAlign = "LEFT"

    table = Table(
        [["Pay Period", pay_period],
         ["Working Days", str(work)],
         ["Present Days", str(present)],
         ["No. of Days Absent", str(absent)]],
        colWidths=[55 * mm, 50 * mm],
    )
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#9E9E9E")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#EFEFEF")),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    table.hAlign = "LEFT"

    doc.build([
        banner,
        Spacer(1, 10),
        Paragraph("SALARY SLIP", title),
        Spacer(1, 10),
        Paragraph("<b>Company:</b> STACX24", styles["Normal"]),
        Paragraph(f"<b>Employee Name:</b> {name}", styles["Normal"]),
        Spacer(1, 16),
        table,
    ])


def main() -> None:
    load_dotenv(ROOT / ".env")

    period = os.environ.get("PAY_PERIOD")
    if period:
        year, month = (int(x) for x in period.split("-"))
    else:
        today = dt.date.today()
        year, month = today.year, today.month

    header, rows = read_summary()
    col = header.index(MONTH_ABBR[month - 1])
    work = working_days(year, month)
    period_label = dt.date(year, month, 1).strftime("%B %Y")

    OUT_DIR.mkdir(exist_ok=True)
    made = []
    for r in rows:
        short = r[0]
        absent = int(r[col]) if col < len(r) and str(r[col]).strip().isdigit() else 0
        name = NAME_MAP.get(short, short)
        present = work - absent
        out = OUT_DIR / f"{name.replace(' ', '_')}_Salary_Slip_{period_label.replace(' ', '_')}.pdf"
        make_slip(name, period_label, work, present, absent, out)
        made.append(out)
        print(f"  {name:<14} WorkingDays={work} Present={present} Absent={absent} -> {out.name}")

    print(f"Pay period: {period_label} | generated {len(made)} salary slip(s) in {OUT_DIR}")

    if "--upload" in sys.argv or os.environ.get("UPLOAD_TO_DRIVE"):
        _upload(made)


def _upload(paths) -> None:
    """Upsert the generated slips into a 'Salary Slips' Drive folder via OAuth."""
    from drive_oauth import find_or_create_folder, get_service, upsert_file

    svc = get_service()
    folder_id = find_or_create_folder(svc, "Salary Slips")
    print(f"Uploading {len(paths)} slip(s) to Drive folder 'Salary Slips' ({folder_id}) ...")
    for p in paths:
        fid, action = upsert_file(svc, p, folder_id)
        print(f"  {action:<8} {p.name}  -> https://drive.google.com/file/d/{fid}/view")


if __name__ == "__main__":
    main()
