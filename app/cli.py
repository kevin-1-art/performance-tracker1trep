"""Command line helpers:  flask --app run <command>"""
import os
import random
from datetime import timedelta

import click
from werkzeug.security import generate_password_hash

from .config import MIN_PASSWORD_LENGTH
from .db import get_db, init_db
from .reporting import (REGISTRATION_TYPES, application_details, build_csv,
                        build_report, build_xlsx)
from .utils import last_completed_week_start, today, week_start_of


def register(app):
    @app.cli.command("init-db")
    def init_db_cmd():
        """Create the tables (safe to run repeatedly)."""
        init_db()
        click.echo("Database ready.")

    @app.cli.command("bootstrap-admin")
    def bootstrap_admin():
        """Create the first admin from environment variables if the database is empty."""
        db = get_db()
        if db.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1").fetchone():
            click.echo("An admin account already exists.")
            return
        if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            raise click.ClickException(
                "The database has users but no admin; create an admin manually."
            )

        username = os.environ.get("INITIAL_ADMIN_USERNAME", "").strip().lower()
        full_name = os.environ.get("INITIAL_ADMIN_FULL_NAME", "").strip()
        password = os.environ.get("INITIAL_ADMIN_PASSWORD", "")
        if not username or not full_name or not password:
            raise click.ClickException(
                "Set INITIAL_ADMIN_USERNAME, INITIAL_ADMIN_FULL_NAME, and "
                "INITIAL_ADMIN_PASSWORD in the service environment."
            )
        if len(password) < 12:
            raise click.ClickException("The initial admin password must be at least 12 characters.")

        db.execute(
            "INSERT INTO users (username, full_name, password_hash, role) VALUES (?,?,?,'admin')",
            (username, full_name, generate_password_hash(password)),
        )
        db.commit()
        click.echo(f"Created initial admin '{username}'.")

    @app.cli.command("create-user")
    @click.option("--username", prompt=True)
    @click.option("--full-name", prompt=True)
    @click.option("--role", type=click.Choice(["admin", "manager", "user"]), default="user")
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    def create_user(username, full_name, role, password):
        """Create an account (use this for your first admin)."""
        if len(password) < MIN_PASSWORD_LENGTH:
            raise click.ClickException(
                f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
            )
        db = get_db()
        db.execute("INSERT INTO users (username, full_name, password_hash, role) VALUES (?,?,?,?)",
                   (username.strip().lower(), full_name, generate_password_hash(password), role))
        db.commit()
        click.echo(f"Created {role} '{username}'.")

    @app.cli.command("seed-demo")
    def seed_demo():
        """Load demo accounts and two weeks of sample applications. DEMO ONLY."""
        if app.config["APP_ENV"] == "production":
            raise click.ClickException("Demo data cannot be seeded in production.")
        db = get_db()
        if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            raise click.ClickException("Database already has users; refusing to seed.")
        rng = random.Random(42)

        def add(username, name, role, manager_id=None, target=0, station=""):
            cur = db.execute(
                "INSERT INTO users (username, full_name, password_hash, role, manager_id, "
                "weekly_target_minor, station) VALUES (?,?,?,?,?,?,?)",
                (username, name, generate_password_hash("Password123!"), role, manager_id,
                 target, station))
            return cur.lastrowid

        add("admin", "System Admin", "admin")
        m1 = add("manager1", "Grace Nakato", "manager", target=500_000_00, station="JINJA")
        m2 = add("manager2", "David Okello", "manager", target=500_000_00, station="KAMPALA")
        staff = [add("alice", "Alice Namuli", "user", m1, 800_000_00, "JINJA"),
                 add("bob", "Bob Ssemakula", "user", m1, 800_000_00, "JINJA"),
                 add("carol", "Carol Achieng", "user", m2, 800_000_00, "KAMPALA"),
                 add("dan", "Dan Mugisha", "user", m2, 800_000_00, "KAMPALA")]
        t = today()
        for uid in staff + [m1, m2]:
            for back in range(1, 15):
                d = t - timedelta(days=back)
                if d.weekday() >= 5:
                    continue
                for _ in range(rng.randint(0, 3)):
                    prn = "2" + "".join(rng.choice("0123456789") for _ in range(12))
                    amt = rng.randint(20, 400) * 1000_00
                    doc_type = rng.choice(REGISTRATION_TYPES)
                    try:
                        db.execute(
                            """INSERT INTO applications
                               (prn, date_of_application, amount_minor, doc_no, doc_type,
                                entity_name, reg_no, user_id)
                               VALUES (?,?,?,?,?,?,?,?)""",
                            (prn, d.isoformat(), amt, f"DOC-{prn}", doc_type,
                             "Demo Business", f"REG-{prn[-6:]}", uid))
                    except Exception:
                        pass
        weeks = {week_start_of(t), week_start_of(t - timedelta(days=7))}
        for uid in staff + [m1, m2]:
            for week in weeks:
                db.execute(
                    """INSERT INTO weekly_activity
                       (user_id, week_start, clients_engaged, radio_talk_shows,
                        joint_field_operations, obrs_accounts_created)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (uid, week.isoformat(), rng.randint(1, 20), rng.randint(0, 3),
                     rng.randint(0, 2), rng.randint(0, 10)))
        db.commit()
        click.echo("Seeded demo accounts with the documented demo password.")

    @app.cli.command("export-weekly")
    def export_weekly():
        """Write last completed week's company-wide report to EXPORT_DIR (for cron / email)."""
        db = get_db()
        admin = db.execute("SELECT * FROM users WHERE role = 'admin' AND is_active = 1 "
                           "ORDER BY id LIMIT 1").fetchone()
        if not admin:
            raise click.ClickException("No admin account exists.")
        week = last_completed_week_start(today())
        report = build_report(db, admin, week)
        details = application_details(db, report["user_ids"], report["week_start"],
                                      report["week_end"])
        out_dir = app.config["EXPORT_DIR"]
        os.makedirs(out_dir, exist_ok=True)
        base = os.path.join(out_dir, f"weekly-performance-{report['week_start']}")
        with open(base + ".xlsx", "wb") as f:
            f.write(build_xlsx(report, details, app.config["CURRENCY"]))
        with open(base + ".csv", "wb") as f:
            f.write(build_csv(report, details))
        click.echo(f"Wrote {base}.xlsx and .csv")
