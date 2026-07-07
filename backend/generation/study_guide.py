"""Study guide generation via the Claude API."""

from datetime import datetime, timezone

from database import get_connection
from .llm_utils import get_client

MODEL = "claude-sonnet-5"

PROMPT_TEMPLATE = """You are creating an in-depth study guide from the following material. Break the
content into clear sections with headers. For each major concept: define it,
explain the underlying mechanism or reasoning (not just the definition), and
include any formulas or worked examples present in the source. Flag anything
that commonly causes confusion. Write for someone preparing for a technical
interview on this material — depth and precision matter more than brevity.

Source material:
{raw_text}
"""


def generate_study_guide_text(raw_text: str) -> str:
    """Call the Claude API to turn raw document text into a markdown study guide."""
    client = get_client()
    message = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        messages=[
            {"role": "user", "content": PROMPT_TEMPLATE.format(raw_text=raw_text)}
        ],
    )
    return "".join(block.text for block in message.content if block.type == "text")


def save_study_guide(document_id: int, user_id: int, content: str) -> int:
    """Persist generated study guide content, linked to its document and owner."""
    generated_at = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO study_guides (document_id, user_id, content, generated_at) VALUES (?, ?, ?, ?)",
            (document_id, user_id, content, generated_at),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_document_raw_text(document_id: int, user_id: int) -> str:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT raw_text FROM documents WHERE id = ? AND user_id = ?", (document_id, user_id)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ValueError(f"No document with id {document_id}")
    return row["raw_text"]


def get_latest_study_guide(document_id: int, user_id: int):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, document_id, content, generated_at FROM study_guides "
            "WHERE document_id = ? AND user_id = ? ORDER BY generated_at DESC LIMIT 1",
            (document_id, user_id),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def generate_and_save_study_guide(document_id: int, user_id: int) -> dict:
    """Fetch a document's raw text, generate a study guide, and save it."""
    raw_text = get_document_raw_text(document_id, user_id)
    content = generate_study_guide_text(raw_text)
    guide_id = save_study_guide(document_id, user_id, content)
    return get_latest_study_guide(document_id, user_id) or {
        "id": guide_id,
        "document_id": document_id,
        "content": content,
    }
