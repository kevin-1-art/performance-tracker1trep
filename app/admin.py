import re
from datetime import date

from flask import (Blueprint, abort, flash, g, redirect, render_template,
                   request, url_for)
from werkzeug.security import generate_password_hash

from .auth import roles_required
from .config import MIN_PASSWORD_LENGTH
from .db import INTEGRITY_ERRORS, database_lock_enabled, get_db
from .reporting import build_report, daily_activity
from .utils import last_completed_week_start, to_minor, today, week_start_of

bp = Blueprint("admin", __name__, url_prefix="/admin")

DATABASE_TABLES = {
    "applications": {
        "label": "Application records",
        "keys": ("id",),
        "fields": ("id", "prn", "date_of_application", "amount_minor", "doc_no",
                   "doc_type", "entity_name", "reg_no", "user_id", "created_at"),
        "form_fields": {
            "prn": "text", "date_of_application": "date", "amount_minor": "number",
            "doc_no": "text", "doc_type": "text", "entity_name": "text", "reg_no": "text",
            "user_id": "number",
        },
    },
    "users": {
        "label": "User accounts",
        "keys": ("id",),
        "fields": ("id", "username", "full_name", "role", "manager_id",
                   "weekly_target_minor", "station", "is_active", "created_at"),
        "form_fields": {
            "username": "text", "full_name": "text", "role": "role",
            "manager_id": "optional_number", "weekly_target_minor": "number",
            "station": "text", "is_active": "checkbox", "password": "password",
        },
    },
    "daily_activity": {
        "label": "Daily activity",
        "keys": ("user_id", "activity_date"),
        "fields": ("user_id", "activity_date", "clients_engaged", "radio_talk_shows",
                   "joint_field_operations", "obrs_accounts_created"),
        "form_fields": {
            "user_id": "number", "activity_date": "date", "clients_engaged": "number",
            "radio_talk_shows": "number", "joint_field_operations": "number",
            "obrs_accounts_created": "number",
        },
    },
    "weekly_activity": {
        "label": "Legacy weekly activity",
        "keys": ("user_id", "week_start"),
        "fields": ("user_id", "week_start", "clients_engaged", "radio_talk_shows",
                   "joint_field_operations", "obrs_accounts_created"),
        "form_fields": {
            "user_id": "number", "week_start": "date", "clients_engaged": "number",
            "radio_talk_shows": "number", "joint_field_operations": "number",
            "obrs_accounts_created": "number",
        },
    },
    "report_download_access": {
        "label": "Report download access",
        "keys": ("user_id",),
        "fields": ("user_id", "granted_by", "granted_at"),
        "form_fields": {"user_id": "number"},
    },
    "download_log": {
        "label": "Download audit log (read-only)",
        "keys": ("id",),
        "fields": ("id", "user_id", "week_start", "format", "downloaded_at"),
        "form_fields": None,
    },
}


@bp.route("/")
@roles_required("admin")
def overview():
    db = get_db()
    this_week = build_report(db, g.user, today())
    last_week = build_report(db, g.user, last_completed_week_start(today()))
    counts = db.execute(
        "SELECT role, COUNT(*) AS n FROM users WHERE is_active = 1 GROUP BY role").fetchall()
    downloads = db.execute(
        """SELECT d.*, u.full_name FROM download_log d JOIN users u ON u.id = d.user_id
           ORDER BY d.id DESC LIMIT 10""").fetchall()
    return render_template("admin/overview.html", this_week=this_week, last_week=last_week,
                           counts={c["role"]: c["n"] for c in counts}, downloads=downloads)


# ---------- all records ----------
@bp.route("/records")
@roles_required("admin")
def records():
    db = get_db()
    q = request.args.get("q", "").strip()
    uid = request.args.get("user", type=int)
    page = max(request.args.get("page", 1, type=int), 1)
    per = 50
    sql = ("SELECT a.*, u.full_name, u.username FROM applications a "
           "JOIN users u ON u.id = a.user_id WHERE 1=1")
    args = []
    if q:
        sql += " AND a.prn LIKE ?"
        args.append(f"%{q.upper()}%")
    if uid:
        sql += " AND a.user_id = ?"
        args.append(uid)
    sql += " ORDER BY a.date_of_application DESC, a.id DESC LIMIT ? OFFSET ?"
    args += [per + 1, (page - 1) * per]
    rows = db.execute(sql, args).fetchall()
    users = db.execute("SELECT id, full_name FROM users WHERE is_active = 1 AND role != 'admin' "
                       "ORDER BY full_name").fetchall()
    activity_date = today()
    activity_week = week_start_of(activity_date)
    form_uid = uid if any(user["id"] == uid for user in users) else (
        users[0]["id"] if users else None
    )
    activity = daily_activity(db, form_uid, activity_date) if form_uid else {}
    return render_template("admin/records.html", records=rows[:per], has_next=len(rows) > per,
                           page=page, users=users, q=q, uid=uid, form_uid=form_uid,
                           activity=activity, activity_date=activity_date,
                           activity_week=activity_week)


# ---------- user management ----------
@bp.route("/users")
@roles_required("admin")
def users():
    rows = get_db().execute(
        """SELECT u.*, m.full_name AS manager_name FROM users u
           LEFT JOIN users m ON m.id = u.manager_id ORDER BY u.role, u.full_name""").fetchall()
    return render_template("admin/users.html", users=rows)


def _managers(db):
    return db.execute("SELECT id, full_name FROM users WHERE role = 'manager' AND is_active = 1 "
                      "ORDER BY full_name").fetchall()


def _read_user_form(existing=None):
    f = request.form
    username = f.get("username", "").strip().lower()
    full_name = f.get("full_name", "").strip()
    role = f.get("role", "user")
    password = f.get("password", "")
    manager_id = f.get("manager_id", type=int) if role == "user" else None
    is_active = 1 if f.get("is_active") else 0
    if not username or not full_name:
        raise ValueError("Username and full name are required.")
    if role not in ("admin", "manager", "user"):
        raise ValueError("Invalid role.")
    if existing is None and len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if existing is not None and password and len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    target = to_minor(f.get("weekly_target") or "0")
    return dict(username=username, full_name=full_name, role=role, password=password,
                manager_id=manager_id, is_active=is_active, target=target,
                station=f.get("station", "").strip())


@bp.route("/users/new", methods=("GET", "POST"))
@roles_required("admin")
def user_new():
    db = get_db()
    if request.method == "POST":
        try:
            d = _read_user_form()
            db.execute(
                """INSERT INTO users (username, full_name, password_hash, role, manager_id,
                                      weekly_target_minor, station, is_active)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (d["username"], d["full_name"], generate_password_hash(d["password"]),
                 d["role"], d["manager_id"], d["target"], d["station"], d["is_active"]))
            db.commit()
            flash(f"Created {d['username']}.", "ok")
            return redirect(url_for("admin.users"))
        except ValueError as e:
            flash(str(e), "error")
        except INTEGRITY_ERRORS:
            flash("That username is already taken.", "error")
    return render_template("admin/user_form.html", u=None, managers=_managers(db))


@bp.route("/users/<int:uid>", methods=("GET", "POST"))
@roles_required("admin")
def user_edit(uid):
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if u is None:
        abort(404)
    if request.method == "POST":
        try:
            d = _read_user_form(existing=u)
            if u["id"] == g.user["id"] and (d["role"] != "admin" or not d["is_active"]):
                raise ValueError("You cannot demote or deactivate your own account.")
            db.execute(
                """UPDATE users SET username = ?, full_name = ?, role = ?, manager_id = ?,
                                    weekly_target_minor = ?, station = ?, is_active = ? WHERE id = ?""",
                (d["username"], d["full_name"], d["role"], d["manager_id"], d["target"],
                 d["station"], d["is_active"], uid))
            if d["password"]:
                db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                           (generate_password_hash(d["password"]), uid))
            db.commit()
            flash("User updated.", "ok")
            return redirect(url_for("admin.users"))
        except ValueError as e:
            flash(str(e), "error")
        except INTEGRITY_ERRORS:
            flash("That username is already taken.", "error")
    return render_template("admin/user_form.html", u=u, managers=_managers(db))


@bp.route("/users/<int:uid>/report-access", methods=("POST",))
@roles_required("admin")
def user_report_access(uid):
    db = get_db()
    user = db.execute(
        "SELECT id, full_name FROM users WHERE id = ? AND is_active = 1 AND role != 'admin'",
        (uid,),
    ).fetchone()
    if user is None:
        abort(404)

    action = request.form.get("action")
    if action == "grant":
        db.execute(
            """INSERT INTO report_download_access (user_id, granted_by)
               VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   granted_by = excluded.granted_by, granted_at = datetime('now')""",
            (uid, g.user["id"]),
        )
        flash(f"{user['full_name']} can now download reports any day.", "ok")
    elif action == "revoke":
        db.execute("DELETE FROM report_download_access WHERE user_id = ?", (uid,))
        flash(f"Any-day report downloads revoked for {user['full_name']}.", "ok")
    else:
        abort(400, "Choose grant or revoke.")
    db.commit()

    params = {"user_id": uid}
    for field in ("from_date", "to_date"):
        value = request.form.get(field, "").strip()
        if value:
            params[field] = value
    return redirect(url_for("reports.index", **params))


@bp.route("/database")
@roles_required("admin")
def database():
    db = get_db()
    return render_template(
        "admin/database.html",
        locked=database_lock_enabled(db),
        tables=DATABASE_TABLES,
    )


@bp.route("/database/toggle", methods=("POST",))
@roles_required("admin")
def database_toggle():
    mode = request.form.get("mode")
    if mode not in ("locked", "unlocked"):
        abort(400, "Choose whether to lock or unlock application database access.")
    db = get_db()
    db.execute(
        """INSERT INTO app_settings (key, value) VALUES ('database_locked', ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        ("1" if mode == "locked" else "0",),
    )
    db.commit()
    flash(
        "Application access is now read-only/paused." if mode == "locked"
        else "Application database access is enabled.",
        "ok",
    )
    return redirect(url_for("admin.database"))


def _database_table(table):
    config = DATABASE_TABLES.get(table)
    if config is None:
        abort(404)
    return config


def _database_key_values(config):
    values = {}
    for key in config["keys"]:
        source = (
            f"original_{key}"
            if request.method == "POST" and request.endpoint == "admin.database_edit"
            else key
        )
        value = request.values.get(source)
        if value is None:
            abort(400, "A row key is required.")
        if key in ("id", "user_id"):
            try:
                value = int(value)
            except ValueError:
                abort(400, "Invalid row key.")
        values[key] = value
    return values


def _database_row(db, table, config, keys):
    where = " AND ".join(f"{key} = ?" for key in config["keys"])
    row = db.execute(
        f"SELECT {', '.join(config['fields'])} FROM {table} WHERE {where}",
        tuple(keys[key] for key in config["keys"]),
    ).fetchone()
    if row is None:
        abort(404)
    return dict(row)


def _database_table_url(table, page=1):
    return url_for("admin.database_table", table=table, page=page)


@bp.route("/database/<table>")
@roles_required("admin")
def database_table(table):
    config = _database_table(table)
    page = max(request.args.get("page", 1, type=int), 1)
    per_page = 50
    fields = ", ".join(config["fields"])
    order = ", ".join(config["keys"])
    rows = get_db().execute(
        f"SELECT {fields} FROM {table} ORDER BY {order} LIMIT ? OFFSET ?",
        (per_page + 1, (page - 1) * per_page),
    ).fetchall()
    entries = []
    for row in rows[:per_page]:
        row = dict(row)
        keys = {key: row[key] for key in config["keys"]}
        entries.append({
            "row": row,
            "edit_url": url_for("admin.database_edit", table=table, **keys)
            if config["form_fields"] is not None else None,
        })
    return render_template(
        "admin/database_table.html",
        table=table,
        config=config,
        rows=entries,
        page=page,
        has_next=len(rows) > per_page,
    )


def _parse_database_form(table, config, existing=None):
    values = {}
    for field, kind in config["form_fields"].items():
        raw = request.form.get(field, "").strip()
        if kind == "password":
            if not raw:
                if existing is None:
                    raise ValueError("A password is required for a new user.")
                continue
            if len(raw) < MIN_PASSWORD_LENGTH:
                raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
            values["password_hash"] = generate_password_hash(raw)
        elif kind == "checkbox":
            values[field] = 1 if request.form.get(field) == "1" else 0
        elif kind == "optional_number" and not raw:
            values[field] = None
        elif kind in ("number", "optional_number"):
            try:
                values[field] = int(raw)
            except ValueError as exc:
                raise ValueError(f"{field.replace('_', ' ').title()} must be a whole number.") from exc
            if values[field] < 0:
                raise ValueError(f"{field.replace('_', ' ').title()} cannot be negative.")
        elif kind == "date":
            try:
                date.fromisoformat(raw)
            except ValueError as exc:
                raise ValueError(f"{field.replace('_', ' ').title()} must be an ISO date.") from exc
            values[field] = raw
        elif kind == "role":
            if raw not in ("admin", "manager", "user"):
                raise ValueError("Choose a valid user role.")
            values[field] = raw
        else:
            values[field] = raw

    if table == "applications":
        if not re.fullmatch(r"\d{13}", values["prn"]):
            raise ValueError("PRNs must contain exactly 13 digits.")
    if table == "users":
        values["username"] = values["username"].lower()
        if not values["username"] or not values["full_name"]:
            raise ValueError("Username and full name are required.")
        if values["role"] != "user":
            values["manager_id"] = None
        elif values["manager_id"] is not None:
            manager = get_db().execute(
                "SELECT id FROM users WHERE id = ? AND role = 'manager' AND is_active = 1",
                (values["manager_id"],),
            ).fetchone()
            if manager is None:
                raise ValueError("Choose an active manager.")
        if existing and existing["id"] == g.user["id"]:
            if values["role"] != "admin" or not values["is_active"]:
                raise ValueError("You cannot demote or deactivate your own account.")
    if table in ("applications", "daily_activity", "weekly_activity"):
        user = get_db().execute(
            "SELECT id FROM users WHERE id = ?", (values["user_id"],)
        ).fetchone()
        if user is None:
            raise ValueError("Choose an existing user.")
    if table in ("daily_activity", "weekly_activity"):
        metric_fields = (
            "clients_engaged", "radio_talk_shows",
            "joint_field_operations", "obrs_accounts_created",
        )
        for field in metric_fields:
            if values[field] < 0:
                raise ValueError(f"{field.replace('_', ' ').title()} cannot be negative.")
    if table == "report_download_access":
        user = get_db().execute(
            "SELECT id FROM users WHERE id = ? AND is_active = 1 AND role != 'admin'",
            (values["user_id"],),
        ).fetchone()
        if user is None:
            raise ValueError("Choose an active non-admin user.")
    return values


def _database_form(table, config, row=None, error=None):
    return render_template(
        "admin/database_form.html",
        table=table,
        config=config,
        row=row,
        error=error,
    )


@bp.route("/database/<table>/new", methods=("GET", "POST"))
@roles_required("admin")
def database_new(table):
    config = _database_table(table)
    if config["form_fields"] is None:
        abort(403)
    if request.method == "POST":
        db = get_db()
        try:
            values = _parse_database_form(table, config)
            if table == "report_download_access":
                values["granted_by"] = g.user["id"]
            fields = tuple(values)
            columns = ", ".join(fields)
            placeholders = ", ".join("?" for _ in fields)
            db.execute(
                f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
                tuple(values[field] for field in fields),
            )
            db.commit()
            flash("Database row created.", "ok")
            return redirect(_database_table_url(table))
        except ValueError as exc:
            db.rollback()
            return _database_form(table, config, error=str(exc))
        except INTEGRITY_ERRORS:
            db.rollback()
            return _database_form(
                table, config, error="The row conflicts with existing data or a related record."
            )
    return _database_form(table, config)


@bp.route("/database/<table>/edit", methods=("GET", "POST"))
@roles_required("admin")
def database_edit(table):
    config = _database_table(table)
    if config["form_fields"] is None:
        abort(403)
    db = get_db()
    keys = _database_key_values(config)
    existing = _database_row(db, table, config, keys)
    if request.method == "POST":
        try:
            values = _parse_database_form(table, config, existing)
            if table == "report_download_access":
                values["granted_by"] = g.user["id"]
                values["granted_at"] = None
            fields = tuple(values)
            assignments = ", ".join(
                f"{field} = datetime('now')" if field == "granted_at" else f"{field} = ?"
                for field in fields
            )
            params = tuple(
                value for field, value in values.items() if field != "granted_at"
            ) + tuple(keys[key] for key in config["keys"])
            where = " AND ".join(f"{key} = ?" for key in config["keys"])
            db.execute(
                f"UPDATE {table} SET {assignments} WHERE {where}",
                params,
            )
            db.commit()
            flash("Database row updated.", "ok")
            return redirect(_database_table_url(table))
        except ValueError as exc:
            db.rollback()
            return _database_form(table, config, row=existing, error=str(exc))
        except INTEGRITY_ERRORS:
            db.rollback()
            return _database_form(
                table, config, row=existing,
                error="The row conflicts with existing data or a related record.",
            )
    return _database_form(table, config, row=existing)


@bp.route("/database/<table>/delete", methods=("POST",))
@roles_required("admin")
def database_delete(table):
    config = _database_table(table)
    if config["form_fields"] is None:
        abort(403)
    if request.form.get("confirm_delete") != "DELETE":
        abort(400, "Type DELETE to confirm removing this row.")
    db = get_db()
    keys = _database_key_values(config)
    row = _database_row(db, table, config, keys)
    if table == "users":
        if row["id"] == g.user["id"]:
            abort(400, "You cannot delete your own account.")
        if row["role"] == "admin":
            admin_count = db.execute(
                "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND is_active = 1"
            ).fetchone()["n"]
            if admin_count <= 1:
                abort(400, "You cannot delete the last active admin.")
    where = " AND ".join(f"{key} = ?" for key in config["keys"])
    try:
        db.execute(
            f"DELETE FROM {table} WHERE {where}",
            tuple(keys[key] for key in config["keys"]),
        )
        db.commit()
        flash("Database row deleted.", "ok")
    except INTEGRITY_ERRORS:
        db.rollback()
        flash("This row is still referenced by related data and cannot be deleted.", "error")
    return redirect(_database_table_url(table))
