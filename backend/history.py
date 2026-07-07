"""Session history and topic-performance aggregation."""

from database import get_connection

NEEDS_REVIEW_THRESHOLD = 70.0


def list_sessions(user_id: int) -> list:
    """Completed sessions for this user, most recent first."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, topics, started_at, ended_at, total_questions, score "
            "FROM sessions WHERE user_id = ? AND ended_at IS NOT NULL ORDER BY started_at DESC",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def topic_performance(user_id: int) -> list:
    """Average score per topic across all of this user's sessions, excluding
    calibration attempts (diagnostic, not part of the scored quiz)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT q.topic AS topic,
                   COUNT(*) AS total_attempts,
                   SUM(CASE a.graded_correct
                       WHEN 'correct' THEN 1.0
                       WHEN 'partial' THEN 0.5
                       ELSE 0.0
                   END) AS weighted_sum
            FROM attempts a
            JOIN questions q ON a.question_id = q.id
            WHERE q.question_type != 'calibration' AND q.user_id = ?
            GROUP BY q.topic
            ORDER BY q.topic
            """,
            (user_id,),
        ).fetchall()
    finally:
        conn.close()

    results = []
    for row in rows:
        total = row["total_attempts"]
        avg_score = round(row["weighted_sum"] / total * 100, 1) if total else None
        results.append({
            "topic": row["topic"],
            "total_attempts": total,
            "average_score": avg_score,
            "needs_review": avg_score is not None and avg_score < NEEDS_REVIEW_THRESHOLD,
        })
    return results
