import os
import secrets

from flask import Flask, abort, redirect, render_template, request, session, url_for

from .config import Config, MIN_PASSWORD_LENGTH
from .utils import fmt_money, is_download_day, next_download_date, today


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)
    if app.config["APP_ENV"] == "production":
        if not app.config["SECRET_KEY"] or app.config["SECRET_KEY"] == "dev-only-change-me":
            raise RuntimeError("Set a unique SECRET_KEY before running in production.")
        app.config["SESSION_COOKIE_SECURE"] = True
    os.makedirs(os.path.dirname(app.config["DATABASE"]), exist_ok=True)

    from . import db
    db.init_app(app)

    from . import admin, auth, cli, manager, records, reports
    app.register_blueprint(auth.bp)
    app.register_blueprint(records.bp)
    app.register_blueprint(manager.bp)
    app.register_blueprint(admin.bp)
    app.register_blueprint(reports.bp)
    cli.register(app)

    # ---------- home: send each role to its own interface ----------
    @app.route("/", endpoint="main.home")
    def home():
        from flask import g
        if g.user is None:
            return redirect(url_for("auth.login"))
        target = {"admin": "admin.overview", "manager": "manager.dashboard"}.get(
            g.user["role"], "records.mine")
        return redirect(url_for(target))

    # ---------- CSRF ----------
    @app.before_request
    def csrf_protect():
        if not app.config["CSRF_ENABLED"]:
            return
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            token = session.get("_csrf")
            sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
            if not token or not sent or not secrets.compare_digest(token, sent):
                abort(400, "Invalid or missing CSRF token. Reload the page and try again.")

    @app.context_processor
    def inject_globals():
        from .reporting import ACTIVITY_ROWS, REGISTRATION_TYPES

        if "_csrf" not in session:
            session["_csrf"] = secrets.token_hex(16)
        return {
            "csrf_token": session["_csrf"],
            "currency": app.config["CURRENCY"],
            "download_day_open": is_download_day(),
            "next_download": next_download_date(),
            "today": today(),
            "registration_types": REGISTRATION_TYPES,
            "activity_rows": ACTIVITY_ROWS,
            "min_password_length": MIN_PASSWORD_LENGTH,
        }

    app.jinja_env.filters["money"] = fmt_money

    # ---------- errors ----------
    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    def http_error(e):
        return render_template("error.html", code=e.code, name=e.name,
                               message=getattr(e, "description", "")), e.code

    @app.errorhandler(500)
    def server_error(_e):
        return render_template(
            "error.html", code=500, name="Internal Server Error",
            message="An unexpected error occurred. Please try again later.",
        ), 500

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'self'; form-action 'self'; "
            "frame-ancestors 'none'; object-src 'none'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'",
        )
        resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        resp.headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")
        resp.headers["Cache-Control"] = "no-store"
        if app.config["SESSION_COOKIE_SECURE"]:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return resp

    return app
