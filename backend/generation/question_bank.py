"""Question bank generation via the Claude API.

Generating questions for every concept in one giant tool call proved unreliable
in testing: with a modest max_tokens the model truncated mid-generation and
silently dropped the whole concepts array; with a larger max_tokens it produced
a malformed JSON string for a big nested array and corrupted the response. So
generation is split into small, independently-reliable calls: one to list
concept names, one per small batch of concepts to write their questions, and
one for calibration questions. The guide content is sent as a cached system
block so the repeated calls aren't repeatedly billed/latent for the same
large context.
"""

import json

from database import get_connection
from .llm_utils import get_client, unwrap_stringified_field

MODEL = "claude-sonnet-5"
CONCEPT_BATCH_SIZE = 4

QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "question_text": {"type": "string"},
        "reference_answer": {"type": "string"},
    },
    "required": ["question_text", "reference_answer"],
}

CONCEPT_LIST_TOOL = {
    "name": "submit_concept_list",
    "description": "Submit the list of major concepts covered in the study guide.",
    "input_schema": {
        "type": "object",
        "properties": {
            "concept_names": {
                "type": "array",
                "description": (
                    "Names of the major, distinct concepts covered in the guide — "
                    "substantial enough to merit their own question, not every subheading."
                ),
                "items": {"type": "string"},
            },
        },
        "required": ["concept_names"],
    },
}

CONCEPT_QUESTIONS_TOOL = {
    "name": "submit_concept_questions",
    "description": "Submit main + follow-up questions for a batch of concepts.",
    "input_schema": {
        "type": "object",
        "properties": {
            "concepts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "concept_name": {"type": "string"},
                        "main_question": QUESTION_SCHEMA,
                        "followup_deeper": QUESTION_SCHEMA,
                        "followup_remedial": QUESTION_SCHEMA,
                    },
                    "required": [
                        "concept_name",
                        "main_question",
                        "followup_deeper",
                        "followup_remedial",
                    ],
                },
            },
        },
        "required": ["concepts"],
    },
}

CALIBRATION_TOOL = {
    "name": "submit_calibration_questions",
    "description": "Submit 3-5 calibration questions spanning easy to hard across the whole document.",
    "input_schema": {
        "type": "object",
        "properties": {
            "calibration_questions": {
                "type": "array",
                "items": QUESTION_SCHEMA,
            },
        },
        "required": ["calibration_questions"],
    },
}

CONCEPT_LIST_PROMPT = """List the major, distinct concepts covered in this study guide — the ones
substantial enough that a student preparing for a technical interview should
be tested on each individually. Don't list every subheading; group closely
related subsections into one concept where that makes sense."""

CONCEPT_QUESTIONS_PROMPT = """For each of the following concepts from the study guide, write:
1. One main open-ended conceptual question testing understanding of the concept.
2. A follow-up question to ask if the student answers the main question correctly —
   it should test deeper understanding, not just repeat the main question.
3. A follow-up question to ask if the student answers the main question incorrectly
   or partially — it should teach or guide them toward the concept, not just
   restate the main question in different words.

For every question, include a short reference_answer — not a rigid exact-match
string, but the key concept(s) a correct answer should demonstrate.

Concepts to write questions for:
{concept_names}"""

CALIBRATION_PROMPT = """Write 3-5 calibration questions spanning easy to hard across the whole
study guide, used to gauge a student's starting difficulty level before a
full quiz. For every question, include a short reference_answer — not a
rigid exact-match string, but the key concept(s) a correct answer should
demonstrate."""


def _call_tool(guide_content: str, prompt: str, tool: dict, tool_name: str, max_tokens: int) -> dict:
    """Call Claude with a forced tool, using a cached system block for the guide content."""
    client = get_client()
    message = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=[
            {
                "type": "text",
                "text": f"Study guide:\n\n{guide_content}",
                "cache_control": {"type": "ephemeral"},
            }
        ],
        tools=[tool],
        tool_choice={"type": "tool", "name": tool_name},
        messages=[{"role": "user", "content": prompt}],
    )
    block = next(b for b in message.content if b.type == "tool_use")
    result = dict(block.input)

    # Defensive: with large nested arrays the model has occasionally emitted a
    # field as a JSON-encoded string instead of a native array. Unwrap if so.
    for key, value in list(result.items()):
        result[key] = unwrap_stringified_field(key, value)
    return result


def _call_tool_with_retry(guide_content: str, prompt: str, tool: dict, tool_name: str, max_tokens: int, retries: int = 2) -> dict:
    last_error = None
    for _ in range(retries + 1):
        try:
            return _call_tool(guide_content, prompt, tool, tool_name, max_tokens)
        except (json.JSONDecodeError, StopIteration) as exc:
            last_error = exc
    raise RuntimeError(f"Failed to get well-formed output from Claude after retries: {last_error}")


def extract_concept_names(guide_content: str) -> list:
    result = _call_tool_with_retry(
        guide_content, CONCEPT_LIST_PROMPT, CONCEPT_LIST_TOOL, "submit_concept_list", max_tokens=1500
    )
    return result["concept_names"]


def generate_concept_batch_questions(guide_content: str, concept_names: list) -> list:
    prompt = CONCEPT_QUESTIONS_PROMPT.format(
        concept_names="\n".join(f"- {name}" for name in concept_names)
    )
    result = _call_tool_with_retry(
        guide_content, prompt, CONCEPT_QUESTIONS_TOOL, "submit_concept_questions", max_tokens=6000
    )
    return result["concepts"]


def generate_calibration_questions(guide_content: str) -> list:
    result = _call_tool_with_retry(
        guide_content, CALIBRATION_PROMPT, CALIBRATION_TOOL, "submit_calibration_questions", max_tokens=2000
    )
    return result["calibration_questions"]


def generate_question_bank_data(guide_content: str) -> dict:
    """Generate the full question bank via several small, reliable calls."""
    concept_names = extract_concept_names(guide_content)

    concepts = []
    for i in range(0, len(concept_names), CONCEPT_BATCH_SIZE):
        batch = concept_names[i:i + CONCEPT_BATCH_SIZE]
        concepts.extend(generate_concept_batch_questions(guide_content, batch))

    calibration_questions = generate_calibration_questions(guide_content)

    return {"concepts": concepts, "calibration_questions": calibration_questions}


def get_document_topic(document_id: int, user_id: int) -> str:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT topic FROM documents WHERE id = ? AND user_id = ?", (document_id, user_id)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ValueError(f"No document with id {document_id}")
    return row["topic"]


def get_study_guide_content(document_id: int, user_id: int) -> str:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT content FROM study_guides WHERE document_id = ? AND user_id = ? "
            "ORDER BY generated_at DESC LIMIT 1",
            (document_id, user_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ValueError(f"No study guide for document {document_id}. Generate one first.")
    return row["content"]


def clear_question_bank(document_id: int, user_id: int, conn) -> None:
    conn.execute("DELETE FROM questions WHERE document_id = ? AND user_id = ?", (document_id, user_id))


def save_question_bank(document_id: int, user_id: int, topic: str, data: dict) -> None:
    """Replace any existing question bank for this document and insert the new one."""
    conn = get_connection()
    try:
        clear_question_bank(document_id, user_id, conn)

        for q in data.get("calibration_questions", []):
            conn.execute(
                "INSERT INTO questions "
                "(document_id, user_id, topic, question_text, question_type, parent_question_id, reference_answer) "
                "VALUES (?, ?, ?, ?, 'calibration', NULL, ?)",
                (document_id, user_id, topic, q["question_text"], q["reference_answer"]),
            )

        for concept in data.get("concepts", []):
            main = concept["main_question"]
            cursor = conn.execute(
                "INSERT INTO questions "
                "(document_id, user_id, topic, question_text, question_type, parent_question_id, reference_answer) "
                "VALUES (?, ?, ?, ?, 'main', NULL, ?)",
                (document_id, user_id, topic, main["question_text"], main["reference_answer"]),
            )
            main_id = cursor.lastrowid

            deeper = concept["followup_deeper"]
            conn.execute(
                "INSERT INTO questions "
                "(document_id, user_id, topic, question_text, question_type, parent_question_id, reference_answer) "
                "VALUES (?, ?, ?, ?, 'followup_deeper', ?, ?)",
                (document_id, user_id, topic, deeper["question_text"], main_id, deeper["reference_answer"]),
            )

            remedial = concept["followup_remedial"]
            conn.execute(
                "INSERT INTO questions "
                "(document_id, user_id, topic, question_text, question_type, parent_question_id, reference_answer) "
                "VALUES (?, ?, ?, ?, 'followup_remedial', ?, ?)",
                (document_id, user_id, topic, remedial["question_text"], main_id, remedial["reference_answer"]),
            )

        conn.commit()
    finally:
        conn.close()


def get_question_bank(document_id: int, user_id: int) -> dict:
    """Fetch the saved question bank for a document, grouped for display."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, document_id, topic, question_text, question_type, "
            "parent_question_id, reference_answer FROM questions "
            "WHERE document_id = ? AND user_id = ? ORDER BY id",
            (document_id, user_id),
        ).fetchall()
    finally:
        conn.close()

    rows = [dict(row) for row in rows]
    calibration = [r for r in rows if r["question_type"] == "calibration"]
    mains = [r for r in rows if r["question_type"] == "main"]
    followups = [r for r in rows if r["question_type"] in ("followup_deeper", "followup_remedial")]

    concepts = []
    for main in mains:
        deeper = next(
            (f for f in followups if f["parent_question_id"] == main["id"] and f["question_type"] == "followup_deeper"),
            None,
        )
        remedial = next(
            (f for f in followups if f["parent_question_id"] == main["id"] and f["question_type"] == "followup_remedial"),
            None,
        )
        concepts.append({"main": main, "followup_deeper": deeper, "followup_remedial": remedial})

    return {"calibration": calibration, "concepts": concepts}


def generate_and_save_question_bank(document_id: int, user_id: int) -> dict:
    """Generate a question bank from a document's study guide and save it."""
    topic = get_document_topic(document_id, user_id)
    guide_content = get_study_guide_content(document_id, user_id)
    data = generate_question_bank_data(guide_content)
    save_question_bank(document_id, user_id, topic, data)
    return get_question_bank(document_id, user_id)
