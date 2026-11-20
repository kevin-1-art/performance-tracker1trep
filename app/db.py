import os
import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    username            TEXT    NOT NULL UNIQUE,
    full_name           TEXT    NOT NULL,
    password_hash       TEXT    NOT NULL,
    role                TEXT    NOT NULL CHECK (role IN ('admin', 'manager', 'user')),
    manager_id          INTEGER REFERENCES users(id) ON DELETE SET NULL,
    weekly_target_minor INTEGER NOT NULL DEFAULT 0,
    station             TEXT    NOT NULL DEFAULT '',
    is_active           INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS applications (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    prn                 TEXT    NOT NULL UNIQUE,
    date_of_application TEXT    NOT NULL,            -- ISO date YYYY-MM-DD
    amount_minor        INTEGER NOT NULL CHECK (amount_minor >= 0),  -- amount x 100
    doc_no              TEXT    NOT NULL DEFAULT '',
    doc_type            TEXT    NOT NULL DEFAULT '',
    entity_name         TEXT    NOT NULL DEFAULT '',
    reg_no              TEXT    NOT NULL DEFAULT '',
    user_id             INTEGER NOT NULL REFERENCES users(id),
    created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_app_user_date ON applications (user_id, date_of_application);
CREATE INDEX IF NOT EXISTS idx_app_date ON applications (date_of_application);

CREATE TABLE IF NOT EXISTS weekly_activity (
    user_id                  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    week_start               TEXT    NOT NULL,
    clients_engaged          INTEGER NOT NULL DEFAULT 0 CHECK (clients_engaged >= 0),
    radio_talk_shows         INTEGER NOT NULL DEFAULT 0 CHECK (radio_talk_shows >= 0),
    joint_field_operations   INTEGER NOT NULL DEFAULT 0 CHECK (joint_field_operations >= 0),
    obrs_accounts_created    INTEGER NOT NULL DEFAULT 0 CHECK (obrs_accounts_created >= 0),
    PRIMARY KEY (user_id, week_start)
);

CREATE TABLE IF NOT EXISTS daily_activity (
    user_id                  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    activity_date            TEXT    NOT NULL,
    clients_engaged          INTEGER NOT NULL DEFAULT 0 CHECK (clients_engaged >= 0),
    radio_talk_shows         INTEGER NOT NULL DEFAULT 0 CHECK (radio_talk_shows >= 0),
    joint_field_operations   INTEGER NOT NULL DEFAULT 0 CHECK (joint_field_operations >= 0),
    obrs_accounts_created    INTEGER NOT NULL DEFAULT 0 CHECK (obrs_accounts_created >= 0),
    PRIMARY KEY (user_id, activity_date)
);

CREATE TABLE IF NOT EXISTS report_download_access (
    user_id     INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    granted_by  INTEGER NOT NULL REFERENCES users(id),
    granted_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS download_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    week_start    TEXT    NOT NULL,
    format        TEXT    NOT NULL,
    downloaded_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    path = current_app.config["DATABASE"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = get_db()
    db.executescript(SCHEMA)
    migrations = {
        "users": {"station": "TEXT NOT NULL DEFAULT ''"},
        "applications": {
            "doc_no": "TEXT NOT NULL DEFAULT ''",
            "doc_type": "TEXT NOT NULL DEFAULT ''",
            "entity_name": "TEXT NOT NULL DEFAULT ''",
            "reg_no": "TEXT NOT NULL DEFAULT ''",
        },
    }
    for table, columns in migrations.items():
        existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
        for column, definition in columns.items():
            if column not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    activity_fields = (
        "clients_engaged", "radio_talk_shows",
        "joint_field_operations", "obrs_accounts_created",
    )
    fields = ", ".join(activity_fields)
    updates = ", ".join(
        f"{field} = daily_activity.{field} + excluded.{field}"
        for field in activity_fields
    )
    legacy_activity = db.execute(
        f"SELECT user_id, week_start, {fields} FROM weekly_activity"
    ).fetchall()
    for row in legacy_activity:
        db.execute(
            f"""INSERT INTO daily_activity (user_id, activity_date, {fields})
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, activity_date) DO UPDATE SET {updates}""",
            (row["user_id"], row["week_start"], *(row[field] for field in activity_fields)),
        )
    db.execute("DELETE FROM weekly_activity")
    db.commit()


def init_app(app):
    app.teardown_appcontext(close_db)
    # Make sure tables exist on first start (idempotent).
    with app.app_context():
        init_db()
