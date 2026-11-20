import functools
import secrets

from flask import (Blueprint, flash, g, redirect, render_template, request,
                   session, url_for, abort)
from werkzeug.security import check_password_hash, generate_password_hash

from .config import MIN_PASSWORD_LENGTH
from .db import get_db

bp = Blueprint("auth", __name__)


@bp.before_app_request
def load_user():
    g.user = None
    uid = session.get("uid")
    if uid:
        g.user = get_db().execute(
            "SELECT * FROM users WHERE id = ? AND is_active = 1", (uid,)
        ).fetchone()
        if g.user is None:
            session.clear()


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)
    return wrapped


def roles_required(*roles):
    def decorator(view):
        @functools.wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if g.user["role"] not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


@bp.route("/login", methods=("GET", "POST"))
def login():
    if g.user:
        return redirect(url_for("main.home"))
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        row = get_db().execute(
            "SELECT * FROM users WHERE username = ? AND is_active = 1", (username,)
        ).fetchone()
        if row and check_password_hash(row["password_hash"], password):
            session.clear()
            session["uid"] = row["id"]
            session["_csrf"] = secrets.token_hex(16)
            return redirect(url_for("main.home"))
        flash("Invalid username or password.", "error")
    return render_template("login.html")


@bp.route("/logout", methods=("POST",))
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/password", methods=("GET", "POST"))
@login_required
def password():
    if request.method == "POST":
        current = request.form.get("current", "")
        new = request.form.get("new", "")
        if not check_password_hash(g.user["password_hash"], current):
            flash("Current password is incorrect.", "error")
        elif len(new) < MIN_PASSWORD_LENGTH:
            flash(f"New password must be at least {MIN_PASSWORD_LENGTH} characters.", "error")
        else:
            db = get_db()
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                       (generate_password_hash(new), g.user["id"]))
            db.commit()
            flash("Password updated.", "ok")
            return redirect(url_for("main.home"))
    return render_template("password.html")
