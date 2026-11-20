"""Weekly performance aggregation and export (pure data code, no routes)."""
import csv
import io
from datetime import timedelta
from pathlib import Path

from openpyxl import Workbook
from openpyxl.drawing.image import Image as SpreadsheetImage
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .utils import week_start_of

REGISTRATION_TYPES = (
    "BUSINESS NAMES",
    "CERTIFICATION",
    "COMPANIES",
    "COMPANY REINSTATEMENTS",
    "CONTINUOUS FILING",
    "ANNUAL RETURN FILING",
    "DATA UPDATE APPLICATIONS",
    "LEGAL DOCUMENTS",
    "RESERVATIONS",
    "SEARCH",
)

REGISTRATION_TYPE_HINTS = {
    "BUSINESS NAMES": "Business name reg*",
    "CERTIFICATION": "Certif*",
    "COMPANIES": "Company reg*",
    "COMPANY REINSTATEMENTS": "Company reinstate*",
    "CONTINUOUS FILING": "Continu*",
    "ANNUAL RETURN FILING": "Annual*",
    "DATA UPDATE APPLICATIONS": "Data*",
    "LEGAL DOCUMENTS": "Legal*Doc*",
    "RESERVATIONS": "*reservation*",
    "SEARCH": "Search*",
}

ACTIVITY_ROWS = (
    ("clients_engaged", "Number of clients engaged"),
    ("radio_talk_shows", "Radio talk shows"),
    ("joint_field_operations", "No. of Joint Field Operations with Stakeholders"),
    ("obrs_accounts_created", "OBRS Accounts created"),
)

def weekly_activity(db, user_id, week_start):
    fields = [field for field, _label in ACTIVITY_ROWS]
    week_end = week_start + timedelta(days=6)
    row = db.execute(
        f"SELECT {', '.join(f'COALESCE(SUM({field}), 0) AS {field}' for field in fields)} "
        "FROM daily_activity WHERE user_id = ? AND activity_date BETWEEN ? AND ?",
        (user_id, week_start.isoformat(), week_end.isoformat())).fetchone()
    return {field: row[field] if row else 0 for field in fields}


def daily_activity(db, user_id, activity_date):
    fields = [field for field, _label in ACTIVITY_ROWS]
    row = db.execute(
        f"SELECT {', '.join(fields)} FROM daily_activity "
        "WHERE user_id = ? AND activity_date = ?",
        (user_id, activity_date.isoformat())).fetchone()
    return {field: row[field] if row else 0 for field in fields}


# ---------- scoping ----------
def scope_users(db, viewer):
    """Users whose performance the viewer is allowed to see."""
    cols = "id, username, full_name, role, weekly_target_minor, station"
    if viewer["role"] == "admin":
        return db.execute(
            f"SELECT {cols} FROM users WHERE is_active = 1 AND role != 'admin' "
            "ORDER BY full_name").fetchall()
    if viewer["role"] == "manager":
        return db.execute(
            f"SELECT {cols} FROM users WHERE is_active = 1 AND (manager_id = ? OR id = ?) "
            "ORDER BY full_name", (viewer["id"], viewer["id"])).fetchall()
    return db.execute(f"SELECT {cols} FROM users WHERE id = ?", (viewer["id"],)).fetchall()


def _in(ids):
    return ",".join("?" * len(ids))


def _aggregate(db, ids, start, end):
    if not ids:
        return {}
    rows = db.execute(
        f"""SELECT user_id, COUNT(*) AS n, COALESCE(SUM(amount_minor), 0) AS total,
                   COUNT(DISTINCT date_of_application) AS days
            FROM applications
            WHERE user_id IN ({_in(ids)}) AND date_of_application BETWEEN ? AND ?
            GROUP BY user_id""",
        (*ids, start.isoformat(), end.isoformat())).fetchall()
    return {r["user_id"]: r for r in rows}


# ---------- report ----------
def build_report(db, viewer, week_start):
    week_start = week_start_of(week_start)
    week_end = week_start + timedelta(days=6)
    return build_range_report(db, viewer, week_start, week_end)


def build_range_report(db, viewer, start, end):
    users = scope_users(db, viewer)
    ids = [u["id"] for u in users]
    duration = end - start

    cur = _aggregate(db, ids, start, end)
    prev = _aggregate(db, ids, start - duration - timedelta(days=1), start - timedelta(days=1))
    activity_fields = [field for field, _label in ACTIVITY_ROWS]
    activity = {}
    if ids:
        sums = ", ".join(f"SUM({field}) AS {field}" for field in activity_fields)
        activity_rows = db.execute(
            f"""SELECT user_id, {sums} FROM daily_activity
                WHERE user_id IN ({_in(ids)}) AND activity_date BETWEEN ? AND ?
                GROUP BY user_id""",
            (*ids, start.isoformat(), end.isoformat())
        ).fetchall()
        activity = {row["user_id"]: row for row in activity_rows}

    rows = []
    for u in users:
        c = cur.get(u["id"])
        p = prev.get(u["id"])
        a = activity.get(u["id"])
        count = c["n"] if c else 0
        total = c["total"] if c else 0
        prev_total = p["total"] if p else 0
        target = u["weekly_target_minor"]
        rows.append({
            "user_id": u["id"],
            "name": u["full_name"],
            "username": u["username"],
            "role": u["role"],
            "applications": count,
            "total_minor": total,
            "avg_minor": (total // count) if count else 0,
            "active_days": c["days"] if c else 0,
            "target_minor": target,
            "achievement_pct": round(total * 100 / target, 1) if target else None,
            "prev_total_minor": prev_total,
            "change_pct": (round((total - prev_total) * 100 / prev_total, 1)
                           if prev_total else None),
            **{field: a[field] if a else 0 for field in activity_fields},
        })
    rows.sort(key=lambda r: (-r["total_minor"], r["name"].lower()))
    rank, last_total = 0, None
    for i, r in enumerate(rows, start=1):
        if r["total_minor"] != last_total:
            rank, last_total = i, r["total_minor"]
        r["rank"] = rank

    daily = []
    daily_map = {}
    if ids:
        for r in db.execute(
            f"""SELECT date_of_application AS d, COUNT(*) AS n, SUM(amount_minor) AS total
                FROM applications
                WHERE user_id IN ({_in(ids)}) AND date_of_application BETWEEN ? AND ?
                GROUP BY date_of_application""",
            (*ids, start.isoformat(), end.isoformat())):
            daily_map[r["d"]] = r
    for i in range(duration.days + 1):
        d = start + timedelta(days=i)
        r = daily_map.get(d.isoformat())
        daily.append({"date": d, "label": d.strftime("%a %d"),
                      "applications": r["n"] if r else 0,
                      "total_minor": r["total"] if r else 0})

    total_apps = sum(r["applications"] for r in rows)
    total_amt = sum(r["total_minor"] for r in rows)
    prev_amt = sum(r["prev_total_minor"] for r in rows)
    target_amt = sum(r["target_minor"] for r in rows)
    totals = {
        "applications": total_apps,
        "total_minor": total_amt,
        "avg_minor": (total_amt // total_apps) if total_apps else 0,
        "prev_total_minor": prev_amt,
        "change_pct": round((total_amt - prev_amt) * 100 / prev_amt, 1) if prev_amt else None,
        "target_minor": target_amt,
        "achievement_pct": round(total_amt * 100 / target_amt, 1) if target_amt else None,
        **{field: sum(row[field] for row in rows) for field in activity_fields},
    }
    return {"week_start": start, "week_end": end, "rows": rows,
            "daily": daily, "totals": totals,
            "max_daily": max([d["total_minor"] for d in daily] + [1]),
            "user_ids": ids}


def application_details(db, user_ids, week_start, week_end):
    if not user_ids:
        return []
    return db.execute(
        f"""SELECT a.doc_no, a.doc_type, a.entity_name, a.reg_no, a.prn,
                   a.date_of_application, a.amount_minor, u.full_name, u.username
            FROM applications a JOIN users u ON u.id = a.user_id
            WHERE a.user_id IN ({_in(user_ids)}) AND a.date_of_application BETWEEN ? AND ?
            ORDER BY a.date_of_application, u.full_name, a.doc_no, a.prn""",
        (*user_ids, week_start.isoformat(), week_end.isoformat())).fetchall()


# ---------- export ----------
SUMMARY_HEAD = ["Rank", "Name", "Username", "Applications", "Total amount", "Average amount",
                "Active days", "Weekly target", "Target achieved %",
                "Previous week", "Change vs previous week %",
                "Clients engaged", "Radio talk shows", "Joint field operations",
                "OBRS accounts created"]
DETAIL_HEAD = ["Document number", "Document type", "Entity name", "Registration number",
               "PRN", "Date of application", "NTR", "Name", "Username"]


def _summary_row(r):
    return [r["rank"], r["name"], r["username"], r["applications"],
            r["total_minor"] / 100, r["avg_minor"] / 100, r["active_days"],
            r["target_minor"] / 100,
            "" if r["achievement_pct"] is None else r["achievement_pct"],
            r["prev_total_minor"] / 100,
            "" if r["change_pct"] is None else r["change_pct"],
            *(r[field] for field, _label in ACTIVITY_ROWS)]


def build_csv(report, details) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"Weekly performance report {report['week_start']} to {report['week_end']}"])
    w.writerow(SUMMARY_HEAD)
    for r in report["rows"]:
        w.writerow(_summary_row(r))
    t = report["totals"]
    w.writerow(["", "TOTAL", "", t["applications"], t["total_minor"] / 100,
                t["avg_minor"] / 100, "", t["target_minor"] / 100,
                "" if t["achievement_pct"] is None else t["achievement_pct"],
                t["prev_total_minor"] / 100,
                "" if t["change_pct"] is None else t["change_pct"],
                *(t[field] for field, _label in ACTIVITY_ROWS)])
    w.writerow([])
    w.writerow(["Applications"])
    w.writerow(DETAIL_HEAD)
    for d in details:
        w.writerow([_safe(d["doc_no"]), _safe(d["doc_type"]), _safe(d["entity_name"]),
                    _safe(d["reg_no"]), _safe(d["prn"]), d["date_of_application"],
                    d["amount_minor"] / 100, _safe(d["full_name"]), d["username"]])
    return buf.getvalue().encode("utf-8-sig")  # BOM so Excel opens UTF-8 correctly


def _safe(value):
    """Neutralise spreadsheet formula injection in text cells."""
    s = str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def build_xlsx(report, details, currency="UGX") -> bytes:
    wb = Workbook()
    head_fill = PatternFill("solid", fgColor="1F3A5F")
    head_font = Font(bold=True, color="FFFFFF")
    money_fmt = "#,##0.00"

    ws = wb.active
    ws.title = "Summary"
    ws["A1"] = f"Weekly performance report: {report['week_start']} to {report['week_end']}"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = f"Amounts in {currency}"
    ws.append([])
    ws.append(SUMMARY_HEAD)
    for c in ws[4]:
        c.fill, c.font = head_fill, head_font
        c.alignment = Alignment(horizontal="center", wrap_text=True)
    for r in report["rows"]:
        ws.append(_summary_row(r))
    t = report["totals"]
    ws.append(["", "TOTAL", "", t["applications"], t["total_minor"] / 100,
               t["avg_minor"] / 100, "", t["target_minor"] / 100,
               "" if t["achievement_pct"] is None else t["achievement_pct"],
               t["prev_total_minor"] / 100,
               "" if t["change_pct"] is None else t["change_pct"],
               *(t[field] for field, _label in ACTIVITY_ROWS)])
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)
    for row in ws.iter_rows(min_row=5):
        for idx in (4, 5, 7, 9):  # money columns (0-based E,F,H,J)
            row[idx].number_format = money_fmt
    _autosize(ws)

    wd = wb.create_sheet("Applications")
    wd.append(DETAIL_HEAD)
    for c in wd[1]:
        c.fill, c.font = head_fill, head_font
    for d in details:
        wd.append([_safe(d["doc_no"]), _safe(d["doc_type"]), _safe(d["entity_name"]),
                   _safe(d["reg_no"]), _safe(d["prn"]), d["date_of_application"],
                   d["amount_minor"] / 100, _safe(d["full_name"]), d["username"]])
    for row in wd.iter_rows(min_row=2, min_col=7, max_col=7):
        row[0].number_format = money_fmt
    wd.freeze_panes = "A2"
    _autosize(wd)

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def build_user_report_xlsx(report, details, activity, station, currency="UGX") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "TREP Report"
    ws.sheet_view.showGridLines = False

    thin = Side(style="thin", color="000000")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    heading_fill = PatternFill("solid", fgColor="D9D9D9")
    bold = Font(name="Times New Roman", bold=True)
    body = Font(name="Times New Roman", size=10)
    money_format = '#,##0.00'

    logo_dir = Path(__file__).parent / "static" / "report_logos"
    logos = (
        ("trep-partner-mark.png", "A1", 285, 223),
        ("ursb.png", "C2", 300, 74),
        ("trep-partner-mark.png", "E1", 285, 223),
    )
    for filename, cell, width, height in logos:
        image = SpreadsheetImage(str(logo_dir / filename))
        image.width = width
        image.height = height
        ws.add_image(image, cell)

    for row in range(1, 9):
        ws.row_dimensions[row].height = 22
    headings = (
        ("C5:D5", "UGANDA REGISTRATION SERVICES BUREAU", bold),
        ("C6:D6", "DIRECTORATE OF BUSINESS REGISTRATION", bold),
        ("C7:D7", (
            f"TREP WEEKLY REPORT FROM {report['week_start']:%d %b %Y} "
            f"to {report['week_end']:%d %b %Y}"
        ), body),
    )
    for cell_range, text, font in headings:
        ws.merge_cells(cell_range)
        cell = ws[cell_range.split(":")[0]]
        cell.value = text
        cell.font = font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    ws["A9"], ws["B9"] = "Name:", report["rows"][0]["name"]
    ws["A10"], ws["B10"] = "Station:", station or "—"
    for cell in ("A9", "A10", "B9", "B10"):
        ws[cell].font = bold

    summary_header = 12
    summary_rows = {
        registration_type: summary_header + index + 1
        for index, registration_type in enumerate(REGISTRATION_TYPES)
    }
    ws.cell(summary_header, 1, "REGISTRATIONS")
    ws.cell(summary_header, 2, "TOTAL")
    ws.cell(summary_header, 3, f"NTR ({currency})")
    ws.cell(summary_header, 4, "Match rule (DOC TYPE)")
    for col in range(1, 5):
        cell = ws.cell(summary_header, col)
        cell.fill = heading_fill
        cell.font = bold
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border

    for registration_type, row in summary_rows.items():
        matching = [detail for detail in details if detail["doc_type"] == registration_type]
        ws.cell(row, 1, registration_type)
        ws.cell(row, 2, len(matching) or "-")
        ws.cell(row, 3, sum(d["amount_minor"] for d in matching) / 100 or "-")
        ws.cell(row, 4, REGISTRATION_TYPE_HINTS[registration_type])
        for col in range(1, 5):
            ws.cell(row, col).font = bold if col == 1 else body
            ws.cell(row, col).border = border
        ws.cell(row, 3).number_format = money_format

    activity_start = summary_header + len(REGISTRATION_TYPES) + 1
    for index, (field, label) in enumerate(ACTIVITY_ROWS):
        row = activity_start + index
        value = activity[field] if activity else 0
        ws.cell(row, 1, label)
        ws.cell(row, 2, value)
        ws.cell(row, 3, "N/A")
        for col in range(1, 4):
            ws.cell(row, col).font = body
            ws.cell(row, col).border = border

    total_row = activity_start + len(ACTIVITY_ROWS)
    ws.cell(total_row, 1, "TOTAL")
    ws.cell(total_row, 2, len(details))
    ws.cell(total_row, 3, sum(d["amount_minor"] for d in details) / 100)
    for col in range(1, 4):
        ws.cell(total_row, col).font = bold
        ws.cell(total_row, col).fill = heading_fill
        ws.cell(total_row, col).border = border
    ws.cell(total_row, 3).number_format = money_format

    missing = sum(
        not all(str(detail[key] or "").strip()
                for key in ("doc_no", "doc_type", "entity_name", "reg_no"))
        for detail in details
    )
    note_row = total_row + 1
    ws.merge_cells(start_row=note_row, start_column=1, end_row=note_row, end_column=4)
    ws.cell(note_row, 1, "TOTAL counts documents only; activity rows are not included.")
    ws.cell(note_row, 1).font = Font(name="Times New Roman", size=10, italic=True)
    ws.merge_cells(start_row=note_row + 1, start_column=1, end_row=note_row + 1, end_column=4)
    ws.cell(note_row + 1, 1, (
        "OK - every entry is counted" if not missing else
        f"Check - {missing} application(s) have incomplete document details"
    ))
    ws.cell(note_row + 1, 1).font = Font(
        name="Times New Roman", bold=True, color="008000" if not missing else "C00000"
    )

    detail_header = note_row + 3
    detail_headers = ["DOC NO.", "DOC TYPE", "DOC NAME/ENTITY NAME", "REG NO.", "PRN", "NTR"]
    for col, header in enumerate(detail_headers, start=1):
        cell = ws.cell(detail_header, col, header)
        cell.fill = heading_fill
        cell.font = bold
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = border
    for row, detail in enumerate(details, start=detail_header + 1):
        values = [detail["doc_no"], detail["doc_type"], detail["entity_name"],
                  detail["reg_no"], detail["prn"], detail["amount_minor"] / 100]
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row, col, _safe(value) if col < 6 else value)
            cell.font = body
            cell.border = border
            cell.alignment = Alignment(vertical="top", wrap_text=True)
        ws.cell(row, 6).number_format = money_format
    ws.freeze_panes = f"A{detail_header + 1}"
    ws.print_title_rows = f"{detail_header}:{detail_header}"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 20
    ws.column_dimensions["C"].width = 30
    ws.column_dimensions["D"].width = 20
    ws.column_dimensions["E"].width = 24
    ws.column_dimensions["F"].width = 21

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _autosize(ws):
    for col in ws.columns:
        letter = get_column_letter(col[0].column)
        width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
        ws.column_dimensions[letter].width = min(max(width + 2, 10), 48)
