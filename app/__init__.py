import os
import secrets

from flask import Flask, abort, redirect, render_template, request, session, url_for

from .config import Config
from .utils import fmt_money, is_download_day, next_download_date, today


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)
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
        }

    app.jinja_env.filters["money"] = fmt_money

    # ---------- errors ----------
    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    def http_error(e):
        return render_template("error.html", code=e.code, name=e.name,
                               message=getattr(e, "description", "")), e.code

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp

    return app
