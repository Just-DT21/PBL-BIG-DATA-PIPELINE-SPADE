"""SQLite persistence layer.

A single small SQLite database holds accounts, sessions, file metadata, jobs,
artifacts and per-user settings. Uploaded bytes never live in the database.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    pw_hash       TEXT NOT NULL,
    pw_salt       TEXT NOT NULL,
    iterations    INTEGER NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    last_seen   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS files (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    original_name   TEXT NOT NULL,
    stored_path     TEXT NOT NULL,
    declared_ext    TEXT NOT NULL DEFAULT '',
    detected_format TEXT NOT NULL DEFAULT '',
    content_type    TEXT NOT NULL DEFAULT 'application/octet-stream',
    size_bytes      INTEGER NOT NULL DEFAULT 0,
    sha256          TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'uploaded',
    analysis_json   TEXT,
    nami_profile_json TEXT,
    nami_policy_id  TEXT,
    error           TEXT,
    created_at      REAL NOT NULL,
    deleted_at      REAL
);
CREATE INDEX IF NOT EXISTS idx_files_user ON files(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    file_id      TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL DEFAULT 'optimize',
    status       TEXT NOT NULL DEFAULT 'queued',
    options_json TEXT NOT NULL DEFAULT '{}',
    result_json  TEXT,
    error        TEXT,
    created_at   REAL NOT NULL,
    started_at   REAL,
    finished_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_file ON jobs(file_id);

CREATE TABLE IF NOT EXISTS artifacts (
    id         TEXT PRIMARY KEY,
    job_id     TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    label      TEXT NOT NULL,
    filename   TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_job ON artifacts(job_id);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS benchmarks (
    id               TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    file_id          TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    status           TEXT NOT NULL DEFAULT 'queued',
    trials_requested INTEGER NOT NULL DEFAULT 3,
    trials_completed INTEGER NOT NULL DEFAULT 0,
    policy_id        TEXT,
    force_policy     TEXT,
    operations_json  TEXT NOT NULL DEFAULT '[]',
    options_json     TEXT NOT NULL DEFAULT '{}',
    profile_json     TEXT,
    result_json      TEXT,
    error            TEXT,
    created_at       REAL NOT NULL,
    started_at       REAL,
    finished_at      REAL
);
CREATE INDEX IF NOT EXISTS idx_benchmarks_user ON benchmarks(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_benchmarks_file ON benchmarks(file_id);

CREATE TABLE IF NOT EXISTS benchmark_artifacts (
    id           TEXT PRIMARY KEY,
    benchmark_id TEXT NOT NULL REFERENCES benchmarks(id) ON DELETE CASCADE,
    user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    label        TEXT NOT NULL,
    filename     TEXT NOT NULL,
    stored_path  TEXT NOT NULL,
    size_bytes   INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_benchmark_artifacts_benchmark ON benchmark_artifacts(benchmark_id);

CREATE TABLE IF NOT EXISTS audit (
    id         TEXT PRIMARY KEY,
    user_id    TEXT,
    action     TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit(user_id, created_at DESC);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(str(path or config.DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


#: Columns added after the first release. ``CREATE TABLE IF NOT EXISTS`` cannot add a
#: column to an existing database, so they are applied as explicit migrations and an
#: existing deployment keeps its data.
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("files", "nami_profile_json", "TEXT"),
    ("files", "nami_policy_id", "TEXT"),
)


def _apply_migrations(conn: sqlite3.Connection) -> list[str]:
    applied: list[str] = []
    for table, column, kind in MIGRATIONS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing or column in existing:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        applied.append(f"{table}.{column}")
    return applied


def init_db(path: Path | None = None) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        _apply_migrations(conn)


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def now() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# Small helpers used by more than one module.
# ---------------------------------------------------------------------------


def audit(conn: sqlite3.Connection, user_id: str | None, action: str, detail: str = "") -> None:
    """Record an action. Details must never contain document contents."""
    conn.execute(
        "INSERT INTO audit (id, user_id, action, detail, created_at) VALUES (?,?,?,?,?)",
        (new_id("aud"), user_id, action, detail[:500], now()),
    )


def get_user_setting(conn: sqlite3.Connection, user_id: str, key: str) -> int:
    default, low, high = config.USER_SETTINGS[key]
    row = conn.execute(
        "SELECT value FROM user_settings WHERE user_id=? AND key=?", (user_id, key)
    ).fetchone()
    if row is None:
        return default
    try:
        value = int(row["value"])
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def all_user_settings(conn: sqlite3.Connection, user_id: str) -> dict[str, int]:
    return {key: get_user_setting(conn, user_id, key) for key in config.USER_SETTINGS}


def set_user_settings(conn: sqlite3.Connection, user_id: str, values: dict[str, Any]) -> dict[str, int]:
    for key, raw in values.items():
        if key not in config.USER_SETTINGS:
            continue
        _default, low, high = config.USER_SETTINGS[key]
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"setting {key} must be an integer") from None
        if not low <= value <= high:
            raise ValueError(f"setting {key} must be between {low} and {high}")
        conn.execute(
            "INSERT INTO user_settings (user_id, key, value) VALUES (?,?,?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value",
            (user_id, key, str(value)),
        )
    return all_user_settings(conn, user_id)
