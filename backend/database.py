"""SQLite schema and connection helpers for the study tool."""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "study_tool.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    filename TEXT,
    topic TEXT,
    raw_text TEXT,
    uploaded_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS study_guides (
    id INTEGER PRIMARY KEY,
    document_id INTEGER,
    content TEXT,
    generated_at TIMESTAMP,
    FOREIGN KEY (document_id) REFERENCES documents(id)
);

CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY,
    document_id INTEGER,
    topic TEXT,
    question_text TEXT,
    question_type TEXT,
    parent_question_id INTEGER,
    reference_answer TEXT,
    FOREIGN KEY (document_id) REFERENCES documents(id)
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    topics TEXT,
    started_at TIMESTAMP,
    ended_at TIMESTAMP,
    total_questions INTEGER,
    score REAL
    -- 'state' column added via migration below: tracks in-progress calibration/
    -- main-quiz position as JSON so a session survives a page refresh or
    -- server restart without needing in-memory process state.
);

CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY,
    session_id INTEGER,
    question_id INTEGER,
    user_answer TEXT,
    graded_correct TEXT,
    feedback TEXT,
    answered_at TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(id),
    FOREIGN KEY (question_id) REFERENCES questions(id)
);
"""


def get_connection() -> sqlite3.Connection:
    """Open a connection to the SQLite database with row access by column name."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, coltype: str) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def init_db() -> None:
    """Create all tables if they don't already exist, and apply additive migrations."""
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        _ensure_column(conn, "sessions", "state", "TEXT")
        # user_id added post-hoc for auth/data-isolation: nullable since it's
        # backfilled by users.claim_orphaned_data() rather than a hard default.
        _ensure_column(conn, "documents", "user_id", "INTEGER")
        _ensure_column(conn, "study_guides", "user_id", "INTEGER")
        _ensure_column(conn, "questions", "user_id", "INTEGER")
        _ensure_column(conn, "sessions", "user_id", "INTEGER")
        conn.commit()
    finally:
        conn.close()
