"""SQLite database connection, production schema, migrations, and backup utilities."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

if getattr(sys, "frozen", False):
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    APP_DIR = Path(sys.executable).resolve().parent
else:
    BUNDLE_DIR = Path(__file__).resolve().parent
    APP_DIR = BUNDLE_DIR

BASE_DIR = APP_DIR
DB_PATH = Path(os.getenv("CLUB_DB_PATH", str(APP_DIR / "club_scheduler.db")))


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS promos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    academic_year TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS schedule_slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    promo_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    time_slot TEXT NOT NULL,
    group_target TEXT NOT NULL,
    session_type TEXT NOT NULL CHECK (session_type IN ('COURS', 'TD', 'TP')),
    module_name TEXT,
    room TEXT,
    FOREIGN KEY (promo_id) REFERENCES promos(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_slots_promo_day_time
    ON schedule_slots (promo_id, day, time_slot);

CREATE INDEX IF NOT EXISTS idx_slots_day_time
    ON schedule_slots (day, time_slot);

CREATE TABLE IF NOT EXISTS members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    department TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'Member',
    promo_id INTEGER NOT NULL,
    group_name TEXT NOT NULL,
    discord TEXT,
    phone TEXT,
    email TEXT,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    shift_exempt INTEGER NOT NULL DEFAULT 0 CHECK (shift_exempt IN (0, 1)),
    notes TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (promo_id) REFERENCES promos(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_members_promo_group
    ON members (promo_id, group_name);

CREATE INDEX IF NOT EXISTS idx_members_department
    ON members (department);

CREATE INDEX IF NOT EXISTS idx_members_active
    ON members (is_active, shift_exempt);

CREATE TABLE IF NOT EXISTS saved_shift_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    event_date TEXT,
    day TEXT NOT NULL,
    department TEXT,
    promo_id INTEGER,
    members_per_shift INTEGER NOT NULL DEFAULT 3,
    allow_cours_fallback INTEGER NOT NULL DEFAULT 1,
    notes TEXT,
    schedule_json TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (promo_id) REFERENCES promos(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS saved_meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    meeting_date TEXT,
    day TEXT NOT NULL,
    time_slot TEXT NOT NULL,
    department TEXT,
    promo_id INTEGER,
    location TEXT,
    notes TEXT,
    expected_free_count INTEGER NOT NULL DEFAULT 0,
    expected_cours_count INTEGER NOT NULL DEFAULT 0,
    total_cohort_count INTEGER NOT NULL DEFAULT 0,
    weighted_score REAL NOT NULL DEFAULT 0.0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (promo_id) REFERENCES promos(id) ON DELETE SET NULL
);
"""

_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "promos": [
        ("academic_year", "TEXT"),
    ],
    "members": [
        ("role", "TEXT NOT NULL DEFAULT 'Member'"),
        ("discord", "TEXT"),
        ("phone", "TEXT"),
        ("email", "TEXT"),
        ("is_active", "INTEGER NOT NULL DEFAULT 1"),
        ("shift_exempt", "INTEGER NOT NULL DEFAULT 0"),
        ("notes", "TEXT"),
        ("created_at", "DATETIME DEFAULT CURRENT_TIMESTAMP"),
    ],
}


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Create a SQLite connection with Row factory and Foreign Key enforcement."""
    target = str(db_path or DB_PATH)
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@contextmanager
def db_session(db_path: Path | str | None = None) -> Generator[sqlite3.Connection, None, None]:
    """Transactional context manager for SQLite operations."""
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_db() -> Generator[sqlite3.Connection, None, None]:
    """FastAPI dependency providing a per-request SQLite connection."""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Apply safe additive column migrations if upgrading an older database file."""
    for table, cols in _MIGRATIONS.items():
        existing = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table});").fetchall()
        }
        for col_name, col_def in cols:
            if col_name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def};")


def init_db(db_path: Path | str | None = None) -> None:
    """Initialize database tables, indexes, and additive migrations on startup."""
    with db_session(db_path) as conn:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript(SCHEMA_SQL)
        _ensure_columns(conn)


def export_sqlite_backup_bytes(db_path: Path | str | None = None) -> bytes:
    """Produce a consistent binary snapshot of the live SQLite database using sqlite3.backup."""
    src_conn = get_connection(db_path)
    tmp_fd, tmp_name = tempfile.mkstemp(suffix=".db")
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    try:
        dst_conn = sqlite3.connect(str(tmp_path))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
        return tmp_path.read_bytes()
    finally:
        src_conn.close()
        tmp_path.unlink(missing_ok=True)
