"""Weekly report page + Thursday-only download."""
from datetime import timedelta

from flask import (Blueprint, Response, abort, current_app, flash, g, redirect,
                   render_template, request, url_for)

from .auth import login_required, roles_required
from .db import get_db
from .reporting import (ACTIVITY_ROWS, application_details, build_csv,
                        build_range_report, build_user_report_xlsx, build_xlsx,
                        daily_activity)
from .utils import is_download_day, next_download_date, parse_date, today, week_start_of

bp = Blueprint("reports", __name__, url_prefix="/reports")

MIME = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv; charset=utf-8",
}


def download_allowed(user, db=None) -> bool:
    if user["role"] == "admin":
        return True
    if is_download_day():
        return True
    return bool(db and db.execute(
        "SELECT 1 FROM report_download_access WHERE user_id = ?", (user["id"],)
    ).fetchone())


def _report_viewer(db):
    if g.user["role"] not in ("admin", "manager") or "user_id" not in request.args:
        return g.user, None
    raw_user_id = request.args["user_id"]
    if not raw_user_id:
        return g.user, None
    try:
        user_id = int(raw_user_id)
    except ValueError:
        abort(400, "Invalid user.")
    scope = ""
    args = [user_id]
    if g.user["role"] == "manager":
        scope = " AND (manager_id = ? OR id = ?)"
        args.extend((g.user["id"], g.user["id"]))
    user = db.execute(
        "SELECT id, username, full_name, role, weekly_target_minor, station FROM users "
        "WHERE id = ? AND is_active = 1 AND role != 'admin'" + scope, args).fetchone()
    if user is None:
        abort(404)
    individual_viewer = dict(user)
    individual_viewer["role"] = "user"
    return individual_viewer, user_id


def _report_period():
    raw_start = request.args.get("from_date", "").strip()
    raw_end = request.args.get("to_date", "").strip()
    if raw_start:
        try:
            start = parse_date(raw_start)
            if raw_end:
                end = parse_date(raw_end)
            else:
                start = week_start_of(start)
                end = start + timedelta(days=6)
        except ValueError:
            abort(400, "Invalid report dates.")
    elif raw_end:
        abort(400, "Choose a from date when specifying a to date.")
    else:
        raw_week = request.args.get("week")
        try:
            start = week_start_of(parse_date(raw_week)) if raw_week else week_start_of(today())
        except ValueError:
            abort(400, "Invalid week.")
        end = start + timedelta(days=6)
    if end < start:
        abort(400, "The to date must be on or after the from date.")
    if (end - start).days > 366:
        abort(400, "Report date ranges cannot exceed one year.")
    return start, end


def _activity_for_period(db, user_id, start, end):
    sums = ", ".join(
        f"COALESCE(SUM({field}), 0) AS {field}" for field, _label in ACTIVITY_ROWS
    )
    row = db.execute(
        f"""SELECT {sums} FROM daily_activity
            WHERE user_id = ? AND activity_date BETWEEN ? AND ?""",
        (user_id, start.isoformat(), end.isoformat())).fetchone()
    return row


def _activity_user_id(db):
    if g.user["role"] == "user":
        return g.user["id"]
    raw_user_id = request.form.get("user_id", "")
    if g.user["role"] == "manager" and not raw_user_id:
        return g.user["id"]
    try:
        user_id = int(raw_user_id)
    except ValueError:
        abort(400, "Choose a valid user for daily activity.")
    scope = ""
    args = [user_id]
    if g.user["role"] == "manager":
        scope = " AND (manager_id = ? OR id = ?)"
        args.extend((g.user["id"], g.user["id"]))
    row = db.execute(
        "SELECT id FROM users WHERE id = ? AND is_active = 1 AND role != 'admin'" + scope,
        args).fetchone()
    if row is None:
            abort(400, "Choose a valid user for daily activity.")
    return user_id


def _activity_return(user_id=None):
    destination = request.form.get("return_to")
    if destination == "records.mine" and g.user["role"] in ("user", "manager"):
        return redirect(url_for("records.mine"))
    if destination == "admin.records" and g.user["role"] == "admin":
        params = {"user": user_id} if user_id else {}
        return redirect(url_for("admin.records", **params))
    return redirect(url_for("reports.index"))


@bp.route("/activity", methods=("POST",))
@roles_required("user", "manager", "admin")
def save_activity():
    db = get_db()
    try:
        activity_date = parse_date(request.form.get("activity_date"))
        counts = {
            field: int(request.form.get(field, "0"))
            for field, _label in ACTIVITY_ROWS
        }
    except (TypeError, ValueError):
        flash("Enter a valid activity date and whole-number report counts.", "error")
        return _activity_return()
    if any(value < 0 for value in counts.values()):
        flash("Report counts cannot be negative.", "error")
        return _activity_return()
    if activity_date > today():
        flash("Activity date cannot be in the future.", "error")
        return _activity_return()

    user_id = _activity_user_id(db)
    fields = [field for field, _label in ACTIVITY_ROWS]
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    updates = ", ".join(f"{field} = excluded.{field}" for field in fields)
    db.execute(
        f"""INSERT INTO daily_activity (user_id, activity_date, {columns})
            VALUES (?, ?, {placeholders})
            ON CONFLICT(user_id, activity_date) DO UPDATE SET {updates}""",
        (user_id, activity_date.isoformat(), *(counts[field] for field in fields)))
    db.commit()
    flash(f"Activity for {activity_date:%A %d %B} saved.", "ok")
    return _activity_return(user_id)


def _can_edit_activity(user, target_user_id):
    return user["role"] != "admin" or target_user_id is not None


def _personal_report(user, target_user_id):
    return target_user_id is not None or user["role"] == "user"


def _period_completed(end):
    return end <= today()


def _period_values():
    start, end = _report_period()
    return start, end


@bp.route("/")
@login_required
def index():
    db = get_db()
    period_start, period_end = _period_values()
    viewer, target_user_id = _report_viewer(db)
    report = build_range_report(db, viewer, period_start, period_end)
    completed = _period_completed(period_end)
    can_download = download_allowed(g.user, db) and (
        completed or (g.user["role"] == "admin" and period_start <= today())
    )
    download_grant = bool(
        g.user["role"] != "admin"
        and db.execute(
            "SELECT 1 FROM report_download_access WHERE user_id = ?", (g.user["id"],)
        ).fetchone()
    )
    selected_user_download_grant = bool(
        g.user["role"] == "admin" and target_user_id
        and db.execute(
            "SELECT 1 FROM report_download_access WHERE user_id = ?", (target_user_id,)
        ).fetchone()
    )
    users = []
    if g.user["role"] == "admin":
        users = db.execute(
            "SELECT id, full_name FROM users WHERE is_active = 1 AND role != 'admin' "
            "ORDER BY full_name").fetchall()
    elif g.user["role"] == "manager":
        users = db.execute(
            """SELECT id, full_name FROM users
               WHERE is_active = 1 AND (manager_id = ? OR id = ?)
               ORDER BY full_name""", (g.user["id"], g.user["id"])).fetchall()
    personal = _personal_report(g.user, target_user_id)
    activity_date = min(period_end, today())
    activity_week = (
        period_start == week_start_of(period_start)
        and period_end == period_start + timedelta(days=6)
    )
    can_edit_activity = (
        _can_edit_activity(g.user, target_user_id)
        and personal and activity_week and period_start <= today()
    )
    activity = (
        daily_activity(db, viewer["id"], activity_date)
        if personal and can_edit_activity else None
    )
    return render_template("reports/index.html", report=report,
                           completed=completed, can_download=can_download,
                           users=users,
                           target_user_id=target_user_id,
                           report_user=viewer if target_user_id else None,
                           period_start=period_start, period_end=period_end,
                           from_date=request.args.get("from_date", ""),
                           to_date=request.args.get("to_date", ""),
                           activity=activity, activity_date=activity_date,
                           activity_week=activity_week,
                           can_edit_activity=can_edit_activity,
                           download_grant=download_grant,
                           selected_user_download_grant=selected_user_download_grant,
                           activity_rows=ACTIVITY_ROWS,
                           personal_report=personal)


@bp.route("/download")
@login_required
def download():
    db = get_db()
    if not download_allowed(g.user, db):
        abort(403, f"Reports can only be downloaded on {_day_name()}. "
                   f"The next download day is {next_download_date():%A %d %B %Y}.")
    fmt = request.args.get("fmt", "xlsx")
    if fmt not in MIME:
        abort(400, "Format must be xlsx or csv.")
    period_start, period_end = _period_values()
    if g.user["role"] == "admin":
        if period_start > today():
            abort(400, "Reports cannot include future dates.")
        period_end = min(period_end, today())
    viewer, target_user_id = _report_viewer(db)
    report = build_range_report(db, viewer, period_start, period_end)
    if not _period_completed(report["week_end"]):
        abort(400, "That report period has not ended yet.")
    details = application_details(db, report["user_ids"], report["week_start"], report["week_end"])

    if fmt == "xlsx":
        if _personal_report(g.user, target_user_id):
            user_id = target_user_id or g.user["id"]
            activity = _activity_for_period(
                db, user_id, report["week_start"], report["week_end"])
            body = build_user_report_xlsx(
                report, details, activity, viewer["station"], current_app.config["CURRENCY"])
        else:
            body = build_xlsx(report, details, current_app.config["CURRENCY"])
    else:
        body = build_csv(report, details)

    db.execute("INSERT INTO download_log (user_id, week_start, format) VALUES (?, ?, ?)",
               (g.user["id"], report["week_start"].isoformat(), fmt))
    db.commit()

    target_suffix = f"-user-{target_user_id}" if target_user_id else ""
    name = (f"weekly-performance{target_suffix}-{report['week_start']}-"
            f"to-{report['week_end']}.{fmt}")
    return Response(body, mimetype=MIME[fmt],
                    headers={"Content-Disposition": f'attachment; filename="{name}"',
                             "Cache-Control": "no-store"})


def _day_name():
    names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    return names[current_app.config["DOWNLOAD_WEEKDAY"]]
