"""Generate monthly STACX24 payslips (PDF) from the Master + Summary tabs.

For a pay-period month it reads each employee's details from the "Master" tab
(Name, Employee ID, Designation, Gross Salary) and their absence from the
"Summary" tab of the attendance Google Sheet, computes pay, and renders an
official payslip PDF per person into Salary_Slips/.

Pay model (matches the STACX24 official payslip):
  Working Days = calendar days in the month      Present Days = Working - Absent
  Per-day pay  = Gross / Working Days            Leave Deduction = Per-day * Absent
  PF = TDS = 0                                   Net = Gross - Total Deductions

Pay period defaults to the PREVIOUS month (payroll pays the completed month);
override with PAY_PERIOD=YYYY-MM. Add --upload to push PDFs to Drive (OAuth).

Run:  .venv/Scripts/python.exe build_salary_slips.py [--upload]
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
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

ROOT = Path(__file__).parent
SPREADSHEET_ID = os.environ.get("ATTENDANCE_SPREADSHEET_ID", "1W3H2uMFG__KTSXDw0trJi71M1RahQS65_bao8EC4sOI")
KEY_PATH = os.environ.get("GOOGLE_SA_KEY_PATH", "credentials/google_credentials.json")
OUT_DIR = ROOT / "Salary_Slips"

COMPANY = "STACX24"
TAGLINE = "Full Stack Development Agency"
PAYMENT_MODE = "Bank Transfer"
CUR = "Rs."  # Helvetica can't render the rupee glyph cleanly, so use "Rs."

# Master short names -> full names for the payslip's Employee Name.
NAME_MAP = {"Soma": "Soma Pani", "Raghul": "Raghul", "Sahil": "Sahil Thakur"}
MONTH_ABBR = [calendar.month_abbr[m] for m in range(1, 13)]


def _open_sheet():
    import gspread
    from google.oauth2.service_account import Credentials

    key = Path(KEY_PATH)
    if not key.is_absolute():  # resolve against the project so cwd doesn't matter
        key = ROOT / key
    creds = Credentials.from_service_account_file(
        str(key), scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return gspread.authorize(creds).open_by_key(SPREADSHEET_ID)


def read_master(sh) -> list[dict]:
    """Return employee rows from the Master tab (in sheet order)."""
    vals = sh.worksheet("Master").get_all_values()
    out = []
    for r in vals[1:]:
        if not r or not r[0].strip():
            continue
        gross = float(str(r[3]).replace(",", "").replace(CUR, "").strip() or 0)
        out.append({"short": r[0].strip(), "emp_id": r[1].strip(),
                    "designation": r[2].strip(), "gross": gross})
    return out


def read_absent(sh, year: int, month: int) -> dict[str, int]:
    """Return {short_name: absence count} for the month from the Summary tab."""
    vals = sh.worksheet("Summary").get_all_values()
    hdr_idx = next(i for i, r in enumerate(vals) if r and r[0] == "Name")
    col = vals[hdr_idx].index(MONTH_ABBR[month - 1])
    out = {}
    for r in vals[hdr_idx + 1:]:
        if r and r[0].strip():
            out[r[0].strip()] = int(r[col]) if col < len(r) and str(r[col]).strip().isdigit() else 0
    return out


def _money(x: float) -> str:
    return f"{x:,.2f}"


def make_payslip(d: dict, out_path: Path) -> None:
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("co", parent=styles["Title"], fontSize=22, spaceAfter=2)
    h2 = ParagraphStyle("ttl", parent=styles["Heading2"], fontSize=14, spaceAfter=0)
    small = ParagraphStyle("sm", parent=styles["Normal"], fontSize=9, textColor=colors.HexColor("#555555"))

    grey = colors.HexColor("#D9D9D9")
    info = Table(
        [["Employee Name", d["name"], "Employee ID", d["emp_id"]],
         ["Designation", d["designation"], "Pay Period", d["period"]],
         ["Working Days", str(d["working"]), "Present Days", str(d["present"])],
         ["Absent Days", str(d["absent"]), "Payment Mode", PAYMENT_MODE]],
        colWidths=[33 * mm, 52 * mm, 33 * mm, 52 * mm],
    )
    info.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#888888")),
        ("BACKGROUND", (0, 0), (0, -1), grey),
        ("BACKGROUND", (2, 0), (2, -1), grey),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))

    money_hdr = f"AMOUNT ({CUR})"
    table = Table(
        [["EARNINGS", money_hdr],
         ["Basic/Gross Salary", _money(d["gross"])],
         ["Total Earnings", _money(d["gross"])],
         ["DEDUCTIONS", money_hdr],
         ["PF", _money(d["pf"])],
         ["TDS", _money(d["tds"])],
         [f"Leave Deduction ({d['absent']} Days)", _money(d["leave_ded"])],
         ["Total Deductions", _money(d["total_ded"])],
         ["NET SALARY PAYABLE", _money(d["net"])]],
        colWidths=[110 * mm, 60 * mm],
    )
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#888888")),
        ("BACKGROUND", (0, 0), (-1, 0), grey),
        ("BACKGROUND", (0, 3), (-1, 3), grey),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 3), (-1, 3), "Helvetica-Bold"),
        ("FONTNAME", (0, 8), (-1, 8), "Helvetica-Bold"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))

    sign = Table([["Employee Signature", "HR/Authorized Signatory"]], colWidths=[85 * mm, 85 * mm])
    sign.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 10),
                              ("ALIGN", (1, 0), (1, 0), "RIGHT")]))

    doc = SimpleDocTemplate(str(out_path), pagesize=A4,
                            topMargin=18 * mm, bottomMargin=18 * mm,
                            leftMargin=20 * mm, rightMargin=20 * mm)
    doc.build([
        Paragraph(COMPANY, h1),
        Paragraph(f"OFFICIAL PAYSLIP - {d['period'].upper()}", h2),
        Paragraph(TAGLINE, small),
        Spacer(1, 12),
        info,
        Spacer(1, 14),
        table,
        Spacer(1, 12),
        Paragraph(f"<b>Net Salary: {CUR} {_money(d['net'])}</b>", styles["Normal"]),
        Paragraph("This is a system-generated salary slip.", small),
        Spacer(1, 26),
        sign,
    ])


def main() -> None:
    load_dotenv(ROOT / ".env")

    period = os.environ.get("PAY_PERIOD")
    if period:
        year, month = (int(x) for x in period.split("-"))
    else:  # default: previous month (payroll pays the completed month)
        first_this = dt.date.today().replace(day=1)
        prev = first_this - dt.timedelta(days=1)
        year, month = prev.year, prev.month
    period_label = dt.date(year, month, 1).strftime("%B %Y")
    working = calendar.monthrange(year, month)[1]  # calendar days in the month

    sh = _open_sheet()
    employees = read_master(sh)
    absents = read_absent(sh, year, month)

    OUT_DIR.mkdir(exist_ok=True)
    made = []
    for e in employees:
        short = e["short"]
        absent = absents.get(short, 0)
        present = working - absent
        per_day = e["gross"] / working if working else 0
        leave_ded = round(per_day * absent, 2)
        total_ded = round(0.0 + 0.0 + leave_ded, 2)
        net = round(e["gross"] - total_ded, 2)
        name = NAME_MAP.get(short, short)
        d = {"name": name, "emp_id": e["emp_id"], "designation": e["designation"],
             "period": period_label, "working": working, "present": present, "absent": absent,
             "gross": e["gross"], "pf": 0.0, "tds": 0.0, "leave_ded": leave_ded,
             "total_ded": total_ded, "total_earn": e["gross"], "net": net}
        out = OUT_DIR / f"{name.replace(' ', '_')}_Payslip_{period_label.replace(' ', '_')}.pdf"
        make_payslip(d, out)
        made.append(out)
        print(f"  {name:<14} ID={e['emp_id']:<6} Gross={_money(e['gross'])} "
              f"Absent={absent} Net={_money(net)} -> {out.name}")

    print(f"Pay period: {period_label} | Working Days={working} | {len(made)} payslip(s) in {OUT_DIR}")

    if "--upload" in sys.argv or os.environ.get("UPLOAD_TO_DRIVE"):
        _upload(made)


def _upload(paths) -> None:
    from drive_oauth import find_or_create_folder, get_service, upsert_file

    svc = get_service()
    folder_id = find_or_create_folder(svc, "Salary Slips")
    print(f"Uploading {len(paths)} payslip(s) to Drive folder 'Salary Slips' ...")
    for p in paths:
        fid, action = upsert_file(svc, p, folder_id)
        print(f"  {action:<8} {p.name} -> https://drive.google.com/file/d/{fid}/view")


if __name__ == "__main__":
    main()
