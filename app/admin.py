import sqlite3

from flask import (Blueprint, abort, flash, g, redirect, render_template,
                   request, url_for)
from werkzeug.security import generate_password_hash

from .auth import roles_required
from .db import get_db
from .reporting import build_report, weekly_activity
from .utils import last_completed_week_start, to_minor, today, week_start_of

bp = Blueprint("admin", __name__, url_prefix="/admin")


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
    activity_week = week_start_of(today())
    form_uid = uid if any(user["id"] == uid for user in users) else (
        users[0]["id"] if users else None
    )
    activity = weekly_activity(db, form_uid, activity_week) if form_uid else {}
    return render_template("admin/records.html", records=rows[:per], has_next=len(rows) > per,
                           page=page, users=users, q=q, uid=uid, form_uid=form_uid,
                           activity=activity, activity_week=activity_week)


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
    if existing is None and len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    if existing is not None and password and len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
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
        except sqlite3.IntegrityError:
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
        except sqlite3.IntegrityError:
            flash("That username is already taken.", "error")
    return render_template("admin/user_form.html", u=u, managers=_managers(db))
