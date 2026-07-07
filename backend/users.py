"""User account storage."""

from datetime import datetime, timezone

from database import get_connection

# Tables that existed before auth was added and now carry a user_id column.
OWNED_TABLES = ("documents", "study_guides", "questions", "sessions")


def create_user(email: str, password_hash: str) -> int:
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
            (email, password_hash, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_user_by_email(email: str):
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def get_user_by_id(user_id: int):
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def count_users() -> int:
    conn = get_connection()
    try:
        return conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    finally:
        conn.close()


def claim_orphaned_data(user_id: int) -> None:
    """Assign any pre-auth data (user_id IS NULL) to the first account created,
    so existing study material isn't silently orphaned by adding auth."""
    conn = get_connection()
    try:
        for table in OWNED_TABLES:
            conn.execute(f"UPDATE {table} SET user_id = ? WHERE user_id IS NULL", (user_id,))
        conn.commit()
    finally:
        conn.close()
