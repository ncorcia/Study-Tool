"""Session runner: calibration, adaptive main-quiz routing, and live grading.

Session progress (which question is next, calibration tallies, the remaining
main-question queue) is persisted as JSON in sessions.state rather than kept
in memory, so an in-progress session survives a page refresh or a dev-server
reload instead of silently losing the student's place.

Every function here takes user_id and bakes it into the SQL WHERE clause
directly, rather than trusting a caller to have already checked ownership —
a session, question, or document that doesn't belong to user_id simply
doesn't match, and looks identical to "doesn't exist".
"""

import json
import random
from datetime import datetime, timezone

from database import get_connection
from generation import grading

MAX_CALIBRATION_QUESTIONS = 5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_topics(user_id: int) -> list:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT topic FROM questions "
            "WHERE user_id = ? AND topic IS NOT NULL AND topic != '' ORDER BY topic",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    return [row["topic"] for row in rows]


def _fetch_question(conn, question_id, user_id: int):
    row = conn.execute(
        "SELECT id, document_id, topic, question_text, question_type, parent_question_id, reference_answer "
        "FROM questions WHERE id = ? AND user_id = ?",
        (question_id, user_id),
    ).fetchone()
    return dict(row) if row else None


def _followup_for(conn, main_question_id: int, followup_type: str, user_id: int):
    row = conn.execute(
        "SELECT id, document_id, topic, question_text, question_type, parent_question_id, reference_answer "
        "FROM questions WHERE parent_question_id = ? AND question_type = ? AND user_id = ?",
        (main_question_id, followup_type, user_id),
    ).fetchone()
    return dict(row) if row else None


def _public_question(question: dict) -> dict:
    """Question payload sent to the frontend before it's answered — never
    includes reference_answer, which would give away the answer key."""
    return {
        "id": question["id"],
        "question_text": question["question_text"],
        "question_type": question["question_type"],
        "topic": question["topic"],
    }


def _compute_tier(state: dict):
    total = state["calibration_correct"] + state["calibration_partial"] + state["calibration_incorrect"]
    if total == 0:
        return None
    frac = (state["calibration_correct"] + 0.5 * state["calibration_partial"]) / total
    if frac >= 0.75:
        return "strong"
    if frac <= 0.35:
        return "weak"
    return "average"


def _advance_to_next_main(state: dict) -> None:
    if state["main_queue"]:
        state["current_question_id"] = state["main_queue"].pop(0)
        state["current_step"] = "main"
        state["phase"] = "main"
    else:
        state["current_question_id"] = None
        state["current_step"] = None
        state["phase"] = "complete"


def _progress(state: dict) -> dict:
    return {
        "calibration_total": len(state["calibration_ids"]),
        "calibration_index": state["calibration_index"],
        "main_total": state["main_total"],
        "main_completed": state["main_completed"],
    }


def _finalize_session(conn, session_id: int, user_id: int, state: dict) -> dict:
    """Score everything except calibration (diagnostic, not part of the quiz score)."""
    rows = conn.execute(
        """
        SELECT a.graded_correct FROM attempts a
        JOIN questions q ON a.question_id = q.id
        WHERE a.session_id = ? AND q.user_id = ? AND q.question_type != 'calibration'
        """,
        (session_id, user_id),
    ).fetchall()

    total = len(rows)
    if total:
        weighted = sum(
            1.0 if r["graded_correct"] == "correct" else 0.5 if r["graded_correct"] == "partial" else 0.0
            for r in rows
        )
        score = round(weighted / total * 100, 1)
    else:
        score = None

    conn.execute(
        "UPDATE sessions SET ended_at = ?, total_questions = ?, score = ?, state = ? WHERE id = ? AND user_id = ?",
        (_now(), total, score, json.dumps(state), session_id, user_id),
    )
    return {"total_questions": total, "score": score, "tier": state.get("tier")}


def start_session(topics: list, user_id: int) -> dict:
    conn = get_connection()
    try:
        placeholders = ",".join("?" for _ in topics)

        calibration_rows = conn.execute(
            f"SELECT id FROM questions WHERE question_type = 'calibration' AND user_id = ? AND topic IN ({placeholders})",
            [user_id] + topics,
        ).fetchall()
        calibration_ids = [r["id"] for r in calibration_rows]
        random.shuffle(calibration_ids)
        calibration_ids = calibration_ids[:MAX_CALIBRATION_QUESTIONS]

        main_rows = conn.execute(
            f"SELECT id FROM questions WHERE question_type = 'main' AND user_id = ? AND topic IN ({placeholders})",
            [user_id] + topics,
        ).fetchall()
        main_ids = [r["id"] for r in main_rows]
        random.shuffle(main_ids)

        state = {
            "phase": "calibration" if calibration_ids else None,
            "calibration_ids": calibration_ids,
            "calibration_index": 0,
            "calibration_correct": 0,
            "calibration_partial": 0,
            "calibration_incorrect": 0,
            "tier": None,
            "main_total": len(main_ids),
            "main_completed": 0,
            "main_queue": main_ids,
            "current_question_id": None,
            "current_step": None,
            "current_main_question_id": None,
        }

        if calibration_ids:
            state["current_question_id"] = calibration_ids[0]
            state["current_step"] = "calibration"
        else:
            _advance_to_next_main(state)

        started_at = _now()
        cursor = conn.execute(
            "INSERT INTO sessions (topics, started_at, ended_at, total_questions, score, state, user_id) "
            "VALUES (?, ?, NULL, 0, NULL, ?, ?)",
            (",".join(topics), started_at, json.dumps(state), user_id),
        )
        session_id = cursor.lastrowid

        if state["phase"] == "complete":
            summary = _finalize_session(conn, session_id, user_id, state)
            conn.commit()
            return {
                "session_id": session_id,
                "session_complete": True,
                "message": "No questions available for the selected topics. Generate a question bank first.",
                **summary,
            }

        conn.commit()
        question = _fetch_question(conn, state["current_question_id"], user_id)
        return {
            "session_id": session_id,
            "session_complete": False,
            "phase": state["phase"],
            "question": _public_question(question),
            "progress": _progress(state),
        }
    finally:
        conn.close()


def get_current(session_id: int, user_id: int) -> dict:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ? AND user_id = ?", (session_id, user_id)
        ).fetchone()
        if row is None:
            raise ValueError(f"No session with id {session_id}")
        state = json.loads(row["state"]) if row["state"] else None

        if row["ended_at"] is not None or state is None or state["phase"] == "complete":
            return {
                "session_complete": True,
                "total_questions": row["total_questions"],
                "score": row["score"],
                "tier": state.get("tier") if state else None,
            }

        question = _fetch_question(conn, state["current_question_id"], user_id)
        return {
            "session_complete": False,
            "phase": state["phase"],
            "question": _public_question(question),
            "progress": _progress(state),
        }
    finally:
        conn.close()


def submit_answer(session_id: int, user_id: int, question_id: int, answer: str) -> dict:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ? AND user_id = ?", (session_id, user_id)
        ).fetchone()
        if row is None:
            raise ValueError(f"No session with id {session_id}")
        if row["ended_at"] is not None:
            raise ValueError("This session has already ended.")

        state = json.loads(row["state"])
        if state["current_question_id"] != question_id:
            raise ValueError("This question is not the current question for this session.")

        question = _fetch_question(conn, question_id, user_id)
        if question is None:
            raise ValueError(f"No question with id {question_id}")

        grade = grading.grade_answer(question["question_text"], question["reference_answer"], answer)

        conn.execute(
            "INSERT INTO attempts (session_id, question_id, user_answer, graded_correct, feedback, answered_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, question_id, answer, grade["graded_correct"], grade["feedback"], _now()),
        )

        concept_reveal = None
        step = state["current_step"]

        if step == "calibration":
            if grade["graded_correct"] == "correct":
                state["calibration_correct"] += 1
            elif grade["graded_correct"] == "partial":
                state["calibration_partial"] += 1
            else:
                state["calibration_incorrect"] += 1
            state["calibration_index"] += 1

            if state["calibration_index"] < len(state["calibration_ids"]):
                state["current_question_id"] = state["calibration_ids"][state["calibration_index"]]
            else:
                state["tier"] = _compute_tier(state)
                _advance_to_next_main(state)

        elif step == "main":
            state["current_main_question_id"] = question_id
            followup_type = "followup_deeper" if grade["graded_correct"] == "correct" else "followup_remedial"
            followup = _followup_for(conn, question_id, followup_type, user_id)
            if followup:
                state["current_step"] = followup_type
                state["current_question_id"] = followup["id"]
            else:
                state["main_completed"] += 1
                _advance_to_next_main(state)

        elif step == "followup_deeper":
            state["main_completed"] += 1
            _advance_to_next_main(state)

        elif step == "followup_remedial":
            main_question = _fetch_question(conn, state["current_main_question_id"], user_id)
            concept_reveal = main_question["reference_answer"] if main_question else None
            state["main_completed"] += 1
            _advance_to_next_main(state)

        session_complete = state["phase"] == "complete"

        result = {
            "graded_correct": grade["graded_correct"],
            "feedback": grade["feedback"],
            "concept_reveal": concept_reveal,
            "session_complete": session_complete,
        }

        if session_complete:
            summary = _finalize_session(conn, session_id, user_id, state)
            result.update(summary)
        else:
            conn.execute(
                "UPDATE sessions SET state = ? WHERE id = ? AND user_id = ?",
                (json.dumps(state), session_id, user_id),
            )
            result["phase"] = state["phase"]
            result["question"] = _public_question(_fetch_question(conn, state["current_question_id"], user_id))
            result["progress"] = _progress(state)

        conn.commit()
        return result
    finally:
        conn.close()
