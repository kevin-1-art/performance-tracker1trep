import os
import re
import sqlite3

from flask import current_app, g
from psycopg import IntegrityError as PostgresIntegrityError
from psycopg import connect
from psycopg.rows import dict_row

INTEGRITY_ERRORS = (sqlite3.IntegrityError, PostgresIntegrityError)

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

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class _PostgresCursor:
    def __init__(self, cursor, buffered_row=None):
        self._cursor = cursor
        self._buffered_row = buffered_row
        self.lastrowid = buffered_row["id"] if buffered_row is not None else None
        self.rowcount = cursor.rowcount

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row

    def fetchone(self):
        if self._buffered_row is not None:
            row, self._buffered_row = self._buffered_row, None
            return row
        return self._cursor.fetchone()

    def fetchall(self):
        rows = self._cursor.fetchall()
        if self._buffered_row is not None:
            rows.insert(0, self._buffered_row)
            self._buffered_row = None
        return rows


class _PostgresDatabase:
    def __init__(self, connection):
        self._connection = connection

    def execute(self, query, params=()):
        query = _postgres_query(query)
        returns_id = bool(re.match(r"\s*INSERT\s+INTO\s+users\b", query, re.IGNORECASE))
        if returns_id and not re.search(r"\bRETURNING\b", query, re.IGNORECASE):
            query = query.rstrip().rstrip(";") + " RETURNING id"
        cursor = self._connection.cursor()
        cursor.execute(query, params)
        buffered_row = cursor.fetchone() if returns_id else None
        return _PostgresCursor(cursor, buffered_row)

    def executemany(self, query, seq_of_params):
        cursor = self._connection.cursor()
        cursor.executemany(_postgres_query(query), seq_of_params)
        return _PostgresCursor(cursor)

    def executescript(self, script):
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        self._connection.close()


def _postgres_query(query):
    query = query.replace("?", "%s")
    return re.sub(
        r"datetime\(\s*'now'\s*\)",
        "CURRENT_TIMESTAMP::text",
        query,
        flags=re.IGNORECASE,
    )


def _database_url():
    url = current_app.config.get("DATABASE_URL")
    if not url and current_app.config["DATABASE"].startswith(("postgres://", "postgresql://")):
        url = current_app.config["DATABASE"]
    if url and url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def get_db():
    if "db" not in g:
        url = _database_url()
        if url:
            g.db = _PostgresDatabase(connect(url, row_factory=dict_row))
        else:
            g.db = sqlite3.connect(current_app.config["DATABASE"])
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def database_lock_enabled(db=None):
    db = db or get_db()
    row = db.execute(
        "SELECT value FROM app_settings WHERE key = ?",
        ("database_locked",),
    ).fetchone()
    return bool(row and row["value"] == "1")


def init_db():
    url = _database_url()
    if not url:
        path = current_app.config["DATABASE"]
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
    db = get_db()
    if url:
        schema = SCHEMA.replace(
            "INTEGER PRIMARY KEY AUTOINCREMENT",
            "INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY",
        ).replace(
            "datetime('now')",
            "CURRENT_TIMESTAMP::text",
        )
    else:
        schema = SCHEMA
    db.executescript(schema)
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
        if url:
            existing = {
                row["name"] for row in db.execute(
                    "SELECT column_name AS name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = ?",
                    (table,),
                )
            }
        else:
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
