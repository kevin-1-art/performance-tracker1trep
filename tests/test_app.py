import csv
import io
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from openpyxl import Workbook, load_workbook
from werkzeug.security import generate_password_hash

from app import create_app
from app.db import _postgres_query, get_db

TZ = ZoneInfo("Africa/Kampala")
WED = datetime(2026, 11, 18, 12, 0, tzinfo=TZ)   # Wednesday
THU = datetime(2026, 11, 19, 12, 0, tzinfo=TZ)   # Thursday


def test_postgres_query_translates_sqlite_placeholders_and_timestamps():
    query = "INSERT INTO users (username, created_at) VALUES (?, datetime('now'))"

    assert _postgres_query(query) == (
        "INSERT INTO users (username, created_at) VALUES (%s, CURRENT_TIMESTAMP::text)"
    )


@pytest.fixture
def clock():
    return {"now": WED}


@pytest.fixture
def app(tmp_path, clock):
    app = create_app({
        "TESTING": True,
        "DATABASE": str(tmp_path / "t.db"),
        "CSRF_ENABLED": False,
        "SECRET_KEY": "test",
        "NOW_FUNC": lambda: clock["now"],
    })
    with app.app_context():
        db = get_db()
        pw = generate_password_hash("Password123!")
        def add(username, role, manager=None, target=0):
            return db.execute(
                "INSERT INTO users (username, full_name, password_hash, role, manager_id, "
                "weekly_target_minor) VALUES (?,?,?,?,?,?)",
                (username, username.title(), pw, role, manager, target)).lastrowid
        add("admin", "admin")
        m1 = add("mgr1", "manager")
        m2 = add("mgr2", "manager")
        add("alice", "user", m1, 100000)
        add("bob", "user", m1)
        add("carol", "user", m2)
        db.commit()
    return app


def test_existing_database_gets_report_columns(tmp_path):
    database = tmp_path / "legacy.db"
    legacy = sqlite3.connect(database)
    legacy.executescript("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, full_name TEXT NOT NULL,
            password_hash TEXT NOT NULL, role TEXT NOT NULL, manager_id INTEGER,
            weekly_target_minor INTEGER NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE applications (
            id INTEGER PRIMARY KEY, prn TEXT NOT NULL UNIQUE,
            date_of_application TEXT NOT NULL, amount_minor INTEGER NOT NULL,
            user_id INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE weekly_activity (
            user_id INTEGER NOT NULL, week_start TEXT NOT NULL,
            clients_engaged INTEGER NOT NULL DEFAULT 0,
            radio_talk_shows INTEGER NOT NULL DEFAULT 0,
            joint_field_operations INTEGER NOT NULL DEFAULT 0,
            obrs_accounts_created INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, week_start)
        );
    """)
    legacy.execute(
        """INSERT INTO users (id, username, full_name, password_hash, role)
           VALUES (1, 'alice', 'Alice', 'hash', 'user')"""
    )
    legacy.execute(
        """INSERT INTO weekly_activity
           (user_id, week_start, clients_engaged, radio_talk_shows,
            joint_field_operations, obrs_accounts_created)
           VALUES (1, '2026-11-13', 8, 2, 1, 4)"""
    )
    legacy.commit()
    legacy.close()
    app = create_app({"TESTING": True, "DATABASE": str(database), "CSRF_ENABLED": False})
    with app.app_context():
        db = get_db()
        user_columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
        app_columns = {row["name"] for row in db.execute("PRAGMA table_info(applications)")}
        activity_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(weekly_activity)")
        }
        assert "station" in user_columns
        assert {"doc_no", "doc_type", "entity_name", "reg_no"} <= app_columns
        assert {
            "clients_engaged", "radio_talk_shows",
            "joint_field_operations", "obrs_accounts_created",
        } <= activity_columns
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='weekly_activity'"
        ).fetchone()
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='daily_activity'"
        ).fetchone()
        migrated = db.execute(
            "SELECT * FROM daily_activity WHERE user_id = 1 AND activity_date = '2026-11-13'"
        ).fetchone()
        assert migrated["clients_engaged"] == 8
        assert migrated["radio_talk_shows"] == 2
        assert db.execute("SELECT COUNT(*) AS n FROM weekly_activity").fetchone()["n"] == 0
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='report_download_access'"
        ).fetchone()


def test_bootstrap_admin_uses_environment_and_is_idempotent(tmp_path, monkeypatch):
    app = create_app({
        "TESTING": True,
        "DATABASE": str(tmp_path / "bootstrap.db"),
        "CSRF_ENABLED": False,
    })
    monkeypatch.setenv("INITIAL_ADMIN_USERNAME", "First.Admin")
    monkeypatch.setenv("INITIAL_ADMIN_FULL_NAME", "First Admin")
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "long-unique-password")

    result = app.test_cli_runner().invoke(args=["bootstrap-admin"])
    assert result.exit_code == 0
    assert "Created initial admin 'first.admin'." in result.output
    assert "long-unique-password" not in result.output

    second_result = app.test_cli_runner().invoke(args=["bootstrap-admin"])
    assert second_result.exit_code == 0
    assert "An admin account already exists." in second_result.output
    with app.app_context():
        admins = get_db().execute(
            "SELECT COUNT(*) AS n FROM users WHERE role='admin'").fetchone()["n"]
        assert admins == 1


def login(client, username):
    r = client.post("/login", data={"username": username, "password": "Password123!"})
    assert r.status_code == 302
    return client


def add_record(client, prn, date, amount, **extra):
    data = {
        "prn": prn,
        "date_of_application": date,
        "amount": amount,
        "doc_no": f"DOC-{prn}",
        "doc_type": "BUSINESS NAMES",
        "entity_name": "Test Entity",
        "reg_no": f"REG-{prn}",
        **extra,
    }
    return client.post("/records/add", data=data, follow_redirects=True)


def make_xlsx(rows):
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)
    return output


# ---------- auth & role routing ----------
def test_login_redirects_by_role(app):
    for user, path in [("admin", "/admin/"), ("mgr1", "/manager/"), ("alice", "/my")]:
        c = login(app.test_client(), user)
        assert c.get("/").headers["Location"].endswith(path)


def test_bad_login(app):
    r = app.test_client().post("/login", data={"username": "alice", "password": "nope"})
    assert b"Invalid username or password" in r.data


def test_pages_require_login(app):
    c = app.test_client()
    for url in ("/my", "/manager/", "/admin/", "/reports/"):
        assert c.get(url).status_code == 302


def test_role_isolation(app):
    alice = login(app.test_client(), "alice")
    assert alice.get("/admin/").status_code == 403
    assert alice.get("/admin/users").status_code == 403
    assert alice.get("/manager/").status_code == 403
    mgr = login(app.test_client(), "mgr1")
    assert mgr.get("/admin/records").status_code == 403


def test_csrf_enforced_when_enabled(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "c.db"), "CSRF_ENABLED": True})
    r = app.test_client().post("/login", data={"username": "x", "password": "y"})
    assert r.status_code == 400


def test_production_rejects_development_secret(tmp_path):
    with pytest.raises(RuntimeError, match="unique SECRET_KEY"):
        create_app({
            "TESTING": True,
            "APP_ENV": "production",
            "SECRET_KEY": "dev-only-change-me",
            "DATABASE": str(tmp_path / "production.db"),
        })


def test_security_headers_and_production_cookie(tmp_path):
    app = create_app({
        "TESTING": True,
        "APP_ENV": "production",
        "SECRET_KEY": "a-unique-test-secret",
        "DATABASE": str(tmp_path / "secure.db"),
    })
    response = app.test_client().get("/login")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Strict-Transport-Security"] == "max-age=31536000"
    assert "Secure" in response.headers["Set-Cookie"]
    assert "HttpOnly" in response.headers["Set-Cookie"]


def test_unexpected_error_does_not_expose_exception(app):
    app.config["PROPAGATE_EXCEPTIONS"] = False
    app.add_url_rule("/test-error", "test_error", lambda: 1 / 0)
    response = app.test_client().get("/test-error")
    assert response.status_code == 500
    assert b"An unexpected error occurred" in response.data
    assert b"ZeroDivisionError" not in response.data


def test_no_api_routes_or_ui_links(app):
    assert not any(rule.rule.startswith("/api/") for rule in app.url_map.iter_rules())
    response = app.test_client().get("/login")
    assert b"/api/" not in response.data


def test_tp_favicon_is_linked_and_served(app):
    client = app.test_client()
    page = client.get("/login")
    assert b"/static/favicon.svg" in page.data
    favicon = client.get("/static/favicon.svg")
    assert favicon.status_code == 200
    assert b">TP</text>" in favicon.data


# ---------- records ----------
def test_add_record_and_duplicate_prn(app):
    c = login(app.test_client(), "alice")
    r = add_record(c, "2260000000123", "2026-11-17", "250,000.50")
    assert b"Recorded PRN 2260000000123" in r.data
    assert b"2260000000123" in c.get("/my").data
    r = login(app.test_client(), "bob")
    r = add_record(r, "2260000000123", "2026-11-17", "1000")
    assert b"already been recorded" in r.data


def test_non_prn_integrity_errors_are_not_reported_as_duplicates(app):
    with app.app_context():
        get_db().execute(
            """CREATE TRIGGER reject_applications BEFORE INSERT ON applications
               BEGIN SELECT RAISE(ABORT, 'unexpected integrity failure'); END""")
    client = login(app.test_client(), "alice")
    with pytest.raises(sqlite3.IntegrityError, match="unexpected integrity failure"):
        add_record(client, "2260000000124", "2026-11-17", "100")


@pytest.mark.parametrize("prn,date,amount,msg", [
    ("12", "2026-11-17", "100", b"PRN must be"),
    ("12345678901234", "2026-11-17", "100", b"PRN must be"),
    ("12345678901A3", "2026-11-17", "100", b"PRN must be"),
    ("2260000000123", "2026-12-25", "100", b"cannot be in the future"),
    ("2260000000123", "17th Nov", "100", b"Date must look like"),
    ("2260000000123", "2026-11-17", "abc", b"Amount must be a number"),
    ("2260000000123", "2026-11-17", "-5", b"cannot be negative"),
])
def test_validation(app, prn, date, amount, msg):
    c = login(app.test_client(), "alice")
    assert msg in add_record(c, prn, date, amount).data


def test_users_only_see_own_records(app):
    a = login(app.test_client(), "alice")
    add_record(a, "2260000000001", "2026-11-17", "100")
    b = login(app.test_client(), "bob")
    assert b"2260000000001" not in b.get("/my").data


def test_cannot_delete_someone_elses_record(app):
    a = login(app.test_client(), "alice")
    add_record(a, "2260000000001", "2026-11-17", "100")
    with app.app_context():
        rid = get_db().execute("SELECT id FROM applications").fetchone()["id"]
    b = login(app.test_client(), "bob")
    assert b.post(f"/records/{rid}/delete").status_code == 403
    admin = login(app.test_client(), "admin")
    assert admin.post(f"/records/{rid}/delete").status_code == 302


def test_owner_can_backfill_migrated_record_details(app):
    client = login(app.test_client(), "alice")
    add_record(client, "2260000000010", "2026-11-17", "100")
    with app.app_context():
        row = get_db().execute("SELECT id FROM applications").fetchone()
        rid = row["id"]
        get_db().execute(
            "UPDATE applications SET doc_no='', doc_type='', entity_name='', reg_no='' WHERE id=?",
            (rid,))
        get_db().commit()
    response = client.post(f"/records/{rid}/edit", data={
        "doc_no": "DOC-10",
        "doc_type": "COMPANIES",
        "entity_name": "Backfilled Entity",
        "reg_no": "REG-10",
    }, follow_redirects=True)
    assert b"Application details updated" in response.data
    with app.app_context():
        row = get_db().execute(
            "SELECT doc_type, entity_name FROM applications WHERE id=?", (rid,)).fetchone()
        assert row["doc_type"] == "COMPANIES"
        assert row["entity_name"] == "Backfilled Entity"


def test_excel_import(app):
    c = login(app.test_client(), "alice")
    data = make_xlsx([
        ["doc_no", "doc_type", "entity_name", "reg_no", "prn", "date_of_application", "amount"],
        ["D1", "BUSINESS NAMES", "Entity 1", "R1", "2260000000001", "2026-11-10", 1000],
        ["D2", "COMPANIES", "Entity 2", "R2", "2260000000002", "11/11/2026", "abc"],
        ["D3", "BUSINESS NAMES", "Entity 3", "R3", "2260000000001", "2026-11-10", 1000],
        ["D4", "SEARCH", "Entity 4", "R4", "2260000000003", "2026-11-12", "3,500"],
    ])
    r = c.post("/records/import", data={"file": (data, "x.xlsx")},
               content_type="multipart/form-data", follow_redirects=True)
    assert b"Imported 2 record(s); 2 skipped" in r.data
    assert b"already recorded" in r.data
    assert b"Amount must be a number" in r.data


def test_csv_import_is_rejected(app):
    c = login(app.test_client(), "alice")
    r = c.post("/records/import", data={
        "file": (io.BytesIO(b"prn,date_of_application,amount\n"), "x.csv")
    }, content_type="multipart/form-data", follow_redirects=True)
    assert b"Upload an .xlsx workbook" in r.data


def test_admin_excel_import_uses_selected_user(app):
    admin = login(app.test_client(), "admin")
    with app.app_context():
        uid = get_db().execute("SELECT id FROM users WHERE username='carol'").fetchone()["id"]
    data = make_xlsx([
        ["DOC NO.", "DOC TYPE", "DOC NAME/ENTITY NAME", "REG NO.", "PRN", "DATE", "NTR"],
        ["D4", "Companies", "Entity 4", "R4", 2260000000004, datetime(2026, 11, 12), 3500],
    ])
    r = admin.post("/records/import", data={"user_id": str(uid), "file": (data, "carol.xlsx")},
                   content_type="multipart/form-data", follow_redirects=True)
    assert b"Imported 1 record(s); 0 skipped" in r.data
    with app.app_context():
        row = get_db().execute(
            "SELECT user_id, amount_minor, doc_type, doc_no, entity_name, reg_no "
            "FROM applications WHERE prn='2260000000004'").fetchone()
        assert row["user_id"] == uid
        assert row["amount_minor"] == 350000
        assert row["doc_type"] == "COMPANIES"
        assert row["doc_no"] == "D4"


def test_admin_adds_for_user_and_sees_all(app):
    admin = login(app.test_client(), "admin")
    with app.app_context():
        uid = get_db().execute("SELECT id FROM users WHERE username='carol'").fetchone()["id"]
    r = add_record(admin, "2260000000005", "2026-11-16", "500", user_id=uid)
    assert b"2260000000005" in r.data
    assert b"Carol" in admin.get("/admin/records").data


# ---------- scoping of reports ----------
def seed_week(app):
    """Last completed reporting week = Fri 6 Nov .. Thu 12 Nov 2026."""
    for u, prn, d, amt in [("alice", "2260000000001", "2026-11-10", "1000"),
                           ("alice", "2260000000002", "2026-11-11", "500"),
                           ("bob", "2260000000003", "2026-11-12", "2000"),
                           ("carol", "2260000000004", "2026-11-12", "9999")]:
        add_record(login(app.test_client(), u), prn, d, amt)


def test_report_scoping(app):
    seed_week(app)
    alice = login(app.test_client(), "alice").get("/reports/?week=2026-11-09").data
    assert b"Alice" in alice and b"Bob" not in alice and b"Carol" not in alice
    mgr = login(app.test_client(), "mgr1").get("/reports/?week=2026-11-09").data
    assert b"Alice" in mgr and b"Bob" in mgr and b"Carol" not in mgr
    admin = login(app.test_client(), "admin").get("/reports/?week=2026-11-09").data
    assert b"Alice" in admin and b"Bob" in admin and b"Carol" in admin


# ---------- Thursday gate ----------
def test_download_blocked_when_not_thursday(app, clock):
    seed_week(app)
    for u in ("alice", "mgr1"):
        c = login(app.test_client(), u)
        r = c.get("/reports/download?week=2026-11-09&fmt=xlsx")
        assert r.status_code == 403, u
        assert b"only be downloaded on Thursday" in r.data
        assert b"Downloads are locked" in c.get("/reports/?week=2026-11-09").data


def test_admin_can_download_a_user_report_any_day(app):
    seed_week(app)
    with app.app_context():
        uid = get_db().execute("SELECT id FROM users WHERE username='alice'").fetchone()["id"]
        get_db().execute("UPDATE users SET station='JINJA' WHERE id=?", (uid,))
        get_db().commit()
    admin = login(app.test_client(), "admin")
    page = admin.get(f"/reports/?week=2026-11-09&user_id={uid}")
    assert page.status_code == 200 and b"Alice" in page.data
    r = admin.get(f"/reports/download?week=2026-11-09&user_id={uid}&fmt=xlsx")
    assert r.status_code == 200
    assert f"user-{uid}".encode() in r.headers["Content-Disposition"].encode()
    wb = load_workbook(io.BytesIO(r.data))
    report = wb["TREP Report"]
    assert len(report._images) == 3
    assert [image.anchor._from.col for image in report._images] == [0, 2, 4]
    assert report["B9"].value == "Alice"
    assert report["B10"].value == "JINJA"
    assert report["A13"].value == "BUSINESS NAMES"
    assert report["B13"].value == 2
    assert report["A31"].value == "DOC NO."


def test_admin_can_download_current_week_user_report_before_thursday(app):
    seed_week(app)
    user = login(app.test_client(), "alice")
    add_record(user, "2260000000099", "2026-11-18", "25")
    with app.app_context():
        uid = get_db().execute(
            "SELECT id FROM users WHERE username='alice'").fetchone()["id"]

    admin = login(app.test_client(), "admin")
    page = admin.get(f"/reports/?week=2026-11-16&user_id={uid}")
    assert b"Excel (.xlsx)" in page.data
    response = admin.get(
        f"/reports/download?week=2026-11-16&user_id={uid}&fmt=xlsx")
    assert response.status_code == 200
    assert "to-2026-11-18.xlsx" in response.headers["Content-Disposition"]
    report = load_workbook(io.BytesIO(response.data))["TREP Report"]
    details = list(report.iter_rows(min_row=32, min_col=5, max_col=5, values_only=True))
    assert ("2260000000099",) in details
    future = admin.get(
        "/reports/download?from_date=2026-11-19&to_date=2026-11-19&fmt=xlsx")
    assert future.status_code == 400


def test_manager_can_download_an_individual_team_member_report(app, clock):
    seed_week(app)
    clock["now"] = THU
    with app.app_context():
        uid = get_db().execute("SELECT id FROM users WHERE username='alice'").fetchone()["id"]
    response = login(app.test_client(), "mgr1").get(
        f"/reports/download?week=2026-11-09&user_id={uid}&fmt=xlsx")
    assert response.status_code == 200
    report = load_workbook(io.BytesIO(response.data))["TREP Report"]
    assert report["B9"].value == "Alice"
    details = list(report.iter_rows(min_row=32, min_col=5, max_col=5, values_only=True))
    assert details == [("2260000000001",), ("2260000000002",)]


def test_users_cannot_select_another_users_report(app, clock):
    seed_week(app)
    clock["now"] = THU
    with app.app_context():
        bob_id = get_db().execute("SELECT id FROM users WHERE username='bob'").fetchone()["id"]
    r = login(app.test_client(), "alice").get(
        f"/reports/download?week=2026-11-09&user_id={bob_id}&fmt=csv")
    assert r.status_code == 200
    assert b"2260000000001" in r.data and b"2260000000003" not in r.data


def test_download_allowed_on_thursday(app, clock):
    seed_week(app)
    clock["now"] = THU
    for u in ("alice", "mgr1", "admin"):
        c = login(app.test_client(), u)
        r = c.get("/reports/download?week=2026-11-09&fmt=xlsx")
        assert r.status_code == 200, u
        assert "spreadsheetml" in r.mimetype
        assert "attachment" in r.headers["Content-Disposition"]
    r = login(app.test_client(), "alice").get("/reports/download?week=2026-11-09&fmt=csv")
    assert r.status_code == 200 and r.mimetype == "text/csv"


def test_thursday_uses_app_timezone_not_utc(app, clock):
    # 01:00 Thursday in Kampala is still Wednesday 22:00 in UTC.
    clock["now"] = datetime(2026, 11, 19, 1, 0, tzinfo=TZ)
    c = login(app.test_client(), "alice")
    assert c.get("/reports/download?week=2026-11-09").status_code == 200
    # 23:30 Thursday Kampala is Thursday; 00:30 Friday is not.
    clock["now"] = datetime(2026, 11, 20, 0, 30, tzinfo=TZ)
    assert c.get("/reports/download?week=2026-11-09").status_code == 403


def test_xlsx_contents_respect_role_scope(app, clock):
    seed_week(app)
    clock["now"] = THU
    with app.app_context():
        db = get_db()
        users = {
            row["username"]: row["id"]
            for row in db.execute("SELECT id, username FROM users")
        }
        db.executemany(
            """INSERT INTO daily_activity
               (user_id, activity_date, clients_engaged, radio_talk_shows,
                joint_field_operations, obrs_accounts_created)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (users["alice"], "2026-11-06", 10, 2, 1, 4),
                (users["alice"], "2026-11-07", 3, 1, 1, 2),
                (users["bob"], "2026-11-06", 5, 1, 0, 2),
            ],
        )
        db.commit()
    r = login(app.test_client(), "mgr1").get("/reports/download?week=2026-11-09&fmt=xlsx")
    wb = load_workbook(io.BytesIO(r.data))
    summary = [row for row in wb["Summary"].iter_rows(values_only=True)]
    names = [row[1] for row in summary if row[1]]
    assert "Alice" in names and "Bob" in names and "Carol" not in names
    header = summary[3]
    client_index = header.index("Clients engaged")
    alice = next(row for row in summary if row[2] == "alice")
    assert alice[client_index:client_index + 4] == (13, 3, 2, 6)
    total_row = next(row for row in summary if row[1] == "TOTAL")
    assert total_row[3] == 3 and total_row[4] == 3500.0   # 1000 + 500 + 2000
    assert total_row[client_index:client_index + 4] == (18, 4, 2, 8)
    prns = [row[4] for row in wb["Applications"].iter_rows(min_row=2, values_only=True)]
    assert sorted(prns) == ["2260000000001", "2260000000002", "2260000000003"]
    csv_response = login(app.test_client(), "mgr1").get(
        "/reports/download?week=2026-11-09&fmt=csv")
    csv_rows = list(csv.reader(io.StringIO(csv_response.data.decode("utf-8-sig"))))
    csv_header = csv_rows[1]
    csv_alice = next(row for row in csv_rows if len(row) > 2 and row[2] == "alice")
    csv_client_index = csv_header.index("Clients engaged")
    assert csv_alice[csv_client_index:csv_client_index + 4] == ["13", "3", "2", "6"]


def test_cannot_download_unfinished_week(app, clock):
    clock["now"] = THU
    c = login(app.test_client(), "alice")
    r = c.get("/reports/download?week=2026-11-23")  # future reporting week
    assert r.status_code == 400


def test_custom_date_range_and_friday_week_are_applied(app):
    seed_week(app)
    alice = login(app.test_client(), "alice")
    weekly = alice.get("/reports/?from_date=2026-11-10")
    assert weekly.status_code == 200
    assert b"2026-11-06 to 2026-11-12" in weekly.data
    custom = alice.get("/reports/?from_date=2026-11-10&to_date=2026-11-10")
    assert custom.status_code == 200
    assert b"2026-11-10 to 2026-11-10" in custom.data


def test_activity_counts_are_saved_and_included_in_personal_report(app):
    seed_week(app)
    with app.app_context():
        uid = get_db().execute("SELECT id FROM users WHERE username='alice'").fetchone()["id"]
    user = login(app.test_client(), "alice")
    dashboard = user.get("/my").data
    assert dashboard.index(b"Daily activity") < dashboard.index(
        b"Bulk import (Excel)")
    page = user.get("/reports/?week=2026-11-09").data
    for field in ("clients_engaged", "radio_talk_shows",
                  "joint_field_operations", "obrs_accounts_created"):
        assert f'name="{field}"'.encode() in page
    assert b'name="registration_business_names"' not in page
    assert b'name="prn"' not in page
    response = user.post("/reports/activity", data={
        "activity_date": "2026-11-10",
        "return_to": "records.mine",
        "clients_engaged": "12",
        "radio_talk_shows": "2",
        "joint_field_operations": "1",
        "obrs_accounts_created": "3",
    })
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/my")
    user.post("/reports/activity", data={
        "activity_date": "2026-11-11",
        "return_to": "records.mine",
        "clients_engaged": "5",
        "radio_talk_shows": "1",
        "joint_field_operations": "0",
        "obrs_accounts_created": "2",
    })
    with app.app_context():
        saved = get_db().execute(
            """SELECT activity_date, clients_engaged, radio_talk_shows,
                      joint_field_operations, obrs_accounts_created
               FROM daily_activity WHERE user_id = ? AND activity_date = '2026-11-10'""", (uid,)
        ).fetchone()
        assert saved["activity_date"] == "2026-11-10"
        assert saved["clients_engaged"] == 12
    admin = login(app.test_client(), "admin")
    admin_page = admin.get("/admin/records").data
    assert admin_page.index(b"Daily activity") < admin_page.index(
        b"Bulk import (Excel)")
    assert b'name="user_id"' in admin_page
    report_response = admin.get(
        f"/reports/download?week=2026-11-09&user_id={uid}&fmt=xlsx")
    workbook = load_workbook(io.BytesIO(report_response.data))
    assert workbook["TREP Report"]["B13"].value == 2
    assert workbook["TREP Report"]["B22"].value == "-"
    assert workbook["TREP Report"]["B23"].value == 17
    assert workbook["TREP Report"]["B24"].value == 3
    assert workbook["TREP Report"]["B25"].value == 1
    assert workbook["TREP Report"]["B26"].value == 5
    assert workbook["TREP Report"]["B27"].value == 2


def test_admin_grants_and_revokes_any_day_downloads_for_user(app):
    seed_week(app)
    with app.app_context():
        uid = get_db().execute(
            "SELECT id FROM users WHERE username='alice'"
        ).fetchone()["id"]

    alice = login(app.test_client(), "alice")
    report_url = "/reports/download?week=2026-11-09&fmt=xlsx"
    assert alice.get(report_url).status_code == 403

    admin = login(app.test_client(), "admin")
    page = admin.get(f"/reports/?week=2026-11-09&user_id={uid}")
    assert b"Allow downloads any day" in page.data
    granted = admin.post(f"/admin/users/{uid}/report-access", data={
        "action": "grant",
        "from_date": "2026-11-06",
        "to_date": "2026-11-12",
    })
    assert granted.status_code == 302
    assert alice.get(report_url).status_code == 200
    assert alice.get("/reports/download?week=2026-11-02&fmt=xlsx").status_code == 200

    admin.post(f"/admin/users/{uid}/report-access", data={"action": "revoke"})
    assert alice.get(report_url).status_code == 403


def test_non_admin_cannot_grant_any_day_report_downloads(app):
    response = login(app.test_client(), "mgr1").post(
        "/admin/users/4/report-access", data={"action": "grant"}
    )
    assert response.status_code == 403


def test_download_is_logged(app, clock):
    seed_week(app)
    clock["now"] = THU
    login(app.test_client(), "alice").get("/reports/download?week=2026-11-09&fmt=csv")
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) AS n FROM download_log").fetchone()["n"] == 1


def test_admin_download_any_day(app):
    seed_week(app)
    c = login(app.test_client(), "admin")
    assert c.get("/reports/download?week=2026-11-09").status_code == 200


# ---------- user management ----------
def test_admin_creates_user_who_can_login(app):
    admin = login(app.test_client(), "admin")
    r = admin.post("/admin/users/new", data={"username": "Newbie", "full_name": "New Bie",
                                             "password": "LongEnoughPassword1!", "role": "user",
                                             "weekly_target": "5000", "station": "JINJA",
                                             "is_active": "1"},
                   follow_redirects=True)
    assert b"Created newbie" in r.data
    with app.app_context():
        assert get_db().execute(
            "SELECT station FROM users WHERE username='newbie'").fetchone()["station"] == "JINJA"
    c = app.test_client()
    assert c.post(
        "/login", data={"username": "newbie", "password": "LongEnoughPassword1!"}
    ).status_code == 302


def test_admin_cannot_create_account_with_short_password(app):
    admin = login(app.test_client(), "admin")
    response = admin.post("/admin/users/new", data={
        "username": "weak-password",
        "full_name": "Weak Password",
        "password": "Short123!",
        "role": "user",
    }, follow_redirects=True)
    assert b"Password must be at least 12 characters" in response.data


def test_admin_cannot_demote_self(app):
    admin = login(app.test_client(), "admin")
    r = admin.post("/admin/users/1", data={"username": "admin", "full_name": "Admin",
                                           "role": "user", "is_active": "1"}, follow_redirects=True)
    assert b"cannot demote or deactivate" in r.data


def test_disabled_user_is_locked_out(app):
    admin = login(app.test_client(), "admin")
    with app.app_context():
        uid = get_db().execute("SELECT id FROM users WHERE username='bob'").fetchone()["id"]
    admin.post(f"/admin/users/{uid}", data={"username": "bob", "full_name": "Bob", "role": "user"})
    r = app.test_client().post("/login", data={"username": "bob", "password": "Password123!"})
    assert b"Invalid username or password" in r.data


def test_formula_injection_neutralised(app, clock):
    c = login(app.test_client(), "alice")
    with app.app_context():
        get_db().execute("UPDATE users SET full_name = '=CMD1234' WHERE username = 'alice'")
        get_db().commit()
    add_record(c, "2260000000001", "2026-11-10", "100")
    clock["now"] = THU
    r = c.get("/reports/download?week=2026-11-09&fmt=csv")
    assert b"'=CMD1234" in r.data
