"""Record entry: the personal interface for each user (and managers' own records)."""
from datetime import date, datetime
from zipfile import BadZipFile

from flask import (Blueprint, abort, flash, g, redirect, render_template,
                   request, url_for)
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from .auth import login_required, roles_required
from .db import get_db
from .reporting import REGISTRATION_TYPES, build_report, weekly_activity
from .utils import last_completed_week_start, today, validate_application, week_start_of

bp = Blueprint("records", __name__)

HEADER_ALIASES = {
    "prn": "prn",
    "payment_reference_number": "prn",
    "doc_no": "doc_no",
    "document_number": "doc_no",
    "doc_type": "doc_type",
    "document_type": "doc_type",
    "doc_name_entity_name": "entity_name",
    "entity_name": "entity_name",
    "registration_number": "reg_no",
    "reg_no": "reg_no",
    "date": "date",
    "date_of_application": "date",
    "application_date": "date",
    "amount": "amount",
    "ntr": "amount",
}


def _return_to():
    return redirect(url_for("admin.records") if g.user["role"] == "admin"
                    else url_for("records.mine"))


def _target_user_id():
    """Who the new record belongs to. Admin picks a user; everyone else = themselves."""
    if g.user["role"] != "admin":
        return g.user["id"]
    try:
        uid = int(request.form.get("user_id", ""))
    except ValueError:
        return None
    row = get_db().execute(
        "SELECT id FROM users WHERE id = ? AND is_active = 1 AND role != 'admin'",
        (uid,)).fetchone()
    return row["id"] if row else None


def _cell_text(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _application_metadata(doc_no, doc_type, entity_name, reg_no):
    doc_no, entity_name, reg_no = map(_cell_text, (doc_no, entity_name, reg_no))
    doc_type = _cell_text(doc_type).upper()
    if not doc_no:
        raise ValueError("Document number is required")
    if doc_type not in REGISTRATION_TYPES:
        raise ValueError("Choose a valid document type")
    if not entity_name:
        raise ValueError("Document name/entity name is required")
    if not reg_no:
        raise ValueError("Registration number is required")
    return doc_no, doc_type, entity_name, reg_no


@bp.route("/my")
@roles_required("user", "manager")
def mine():
    db = get_db()
    this_week = build_report(db, g.user, today())
    last_week = build_report(db, g.user, last_completed_week_start(today()))
    week_start = this_week["week_start"]
    activity = weekly_activity(db, g.user["id"], week_start)
    page = max(request.args.get("page", 1, type=int), 1)
    per = 25
    rows = db.execute(
        """SELECT * FROM applications WHERE user_id = ?
           ORDER BY date_of_application DESC, id DESC LIMIT ? OFFSET ?""",
        (g.user["id"], per + 1, (page - 1) * per)).fetchall()
    return render_template("user/dashboard.html",
                           me=this_week["rows"][0] if this_week["rows"] else None,
                           last_me=last_week["rows"][0] if last_week["rows"] else None,
                           this_week=this_week, last_week=last_week,
                           activity=activity, activity_week=week_start,
                           records=rows[:per], has_next=len(rows) > per, page=page)


@bp.route("/records/add", methods=("POST",))
@roles_required("user", "manager", "admin")
def add():
    uid = _target_user_id()
    if uid is None:
        flash("Choose a valid user for this record.", "error")
        return _return_to()
    try:
        prn, iso, minor = validate_application(
            request.form.get("prn"), request.form.get("date_of_application"),
            request.form.get("amount"))
        doc_no, doc_type, entity_name, reg_no = _application_metadata(
            request.form.get("doc_no"), request.form.get("doc_type"),
            request.form.get("entity_name"), request.form.get("reg_no"))
    except ValueError as e:
        flash(str(e), "error")
        return _return_to()
    db = get_db()
    cursor = db.execute(
        """INSERT INTO applications
           (prn, date_of_application, amount_minor, doc_no, doc_type, entity_name, reg_no, user_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(prn) DO NOTHING""",
        (prn, iso, minor, doc_no, doc_type, entity_name, reg_no, uid))
    db.commit()
    if cursor.rowcount:
        flash(f"Recorded PRN {prn}.", "ok")
    else:
        flash(f"PRN {prn} has already been recorded.", "error")
    return _return_to()


@bp.route("/records/import", methods=("POST",))
@roles_required("user", "manager", "admin")
def import_excel():
    uid = _target_user_id()
    if uid is None:
        flash("Choose a valid user for this import.", "error")
        return _return_to()
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith(".xlsx"):
        flash("Upload an .xlsx workbook containing the required application details.", "error")
        return _return_to()
    try:
        workbook = load_workbook(f, read_only=True, data_only=True)
    except (BadZipFile, InvalidFileException, OSError, ValueError):
        flash("Could not read the Excel workbook. Upload a valid .xlsx file.", "error")
        return _return_to()

    sheet = workbook.active
    rows = sheet.iter_rows(values_only=True)
    headers = next(rows, None)
    if headers is None:
        workbook.close()
        flash("Excel workbook is empty.", "error")
        return _return_to()
    mapping = {}
    for index, name in enumerate(headers):
        normalized = "_".join(
            str(name or "").strip().lower().replace(".", "").replace("/", " ").split())
        key = HEADER_ALIASES.get(normalized)
        if key:
            if key in mapping:
                workbook.close()
                flash(f"Excel has more than one column for {key}.", "error")
                return _return_to()
            mapping[key] = index
    required = {"prn", "date", "amount", "doc_no", "doc_type", "entity_name", "reg_no"}
    if set(mapping) != required:
        workbook.close()
        flash("Excel needs document number/type, entity name, registration number, PRN, date, and NTR columns.",
              "error")
        return _return_to()

    db = get_db()
    added, errors = 0, []
    try:
        for line_no, row in enumerate(rows, start=2):
            if not any(value is not None and str(value).strip() for value in row):
                continue
            try:
                prn, iso, minor = validate_application(
                    row[mapping["prn"]] if mapping["prn"] < len(row) else None,
                    row[mapping["date"]] if mapping["date"] < len(row) else None,
                    row[mapping["amount"]] if mapping["amount"] < len(row) else None)
                doc_no, doc_type, entity_name, reg_no = _application_metadata(
                    row[mapping["doc_no"]] if mapping["doc_no"] < len(row) else None,
                    row[mapping["doc_type"]] if mapping["doc_type"] < len(row) else None,
                    row[mapping["entity_name"]] if mapping["entity_name"] < len(row) else None,
                    row[mapping["reg_no"]] if mapping["reg_no"] < len(row) else None)
                cursor = db.execute(
                    """INSERT INTO applications
                       (prn, date_of_application, amount_minor, doc_no, doc_type,
                        entity_name, reg_no, user_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(prn) DO NOTHING""",
                    (prn, iso, minor, doc_no, doc_type, entity_name, reg_no, uid))
                if cursor.rowcount:
                    added += 1
                else:
                    errors.append(f"line {line_no}: PRN {prn} already recorded")
            except ValueError as e:
                errors.append(f"line {line_no}: {e}")
    finally:
        workbook.close()
    db.commit()
    flash(f"Imported {added} record(s); {len(errors)} skipped.", "ok" if added else "error")
    for msg in errors[:10]:
        flash(msg, "error")
    if len(errors) > 10:
        flash(f"...and {len(errors) - 10} more problems.", "error")
    return _return_to()


@bp.route("/records/<int:rid>/edit", methods=("GET", "POST"))
@login_required
def edit(rid):
    db = get_db()
    row = db.execute("SELECT * FROM applications WHERE id = ?", (rid,)).fetchone()
    if row is None:
        abort(404)
    if g.user["role"] != "admin" and row["user_id"] != g.user["id"]:
        abort(403)
    if request.method == "POST":
        try:
            doc_no, doc_type, entity_name, reg_no = _application_metadata(
                request.form.get("doc_no"), request.form.get("doc_type"),
                request.form.get("entity_name"), request.form.get("reg_no"))
        except ValueError as e:
            flash(str(e), "error")
        else:
            db.execute(
                "UPDATE applications SET doc_no = ?, doc_type = ?, entity_name = ?, reg_no = ? "
                "WHERE id = ?", (doc_no, doc_type, entity_name, reg_no, rid))
            db.commit()
            flash("Application details updated.", "ok")
            return _return_to()
    return render_template("records/edit.html", record=row)


@bp.route("/records/<int:rid>/delete", methods=("POST",))
@login_required
def delete(rid):
    db = get_db()
    row = db.execute("SELECT * FROM applications WHERE id = ?", (rid,)).fetchone()
    if row is None:
        abort(404)
    if g.user["role"] != "admin" and row["user_id"] != g.user["id"]:
        abort(403)
    db.execute("DELETE FROM applications WHERE id = ?", (rid,))
    db.commit()
    flash(f"Deleted PRN {row['prn']}.", "ok")
    return _return_to()
