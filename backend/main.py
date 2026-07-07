"""FastAPI app entry point for the study tool."""

import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# Loaded before any first-party import below: database.py reads DATA_DIR from
# the environment at import time, so .env must already be loaded by then for
# local dev to pick it up. A real deploy env var doesn't have this hazard
# (it's set before the process starts), but this keeps the two paths consistent.
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

import auth
import history
import session_runner
import users as users_db
from database import DATA_DIR, get_connection, init_db
from generation import question_bank, study_guide
from parsers import docx_parser, pdf_parser

UPLOADS_DIR = DATA_DIR / "uploads"
FRONTEND_DIR = BASE_DIR / "frontend"

SESSION_SECRET_KEY = os.environ.get("SESSION_SECRET_KEY")
if not SESSION_SECRET_KEY:
    raise RuntimeError(
        "SESSION_SECRET_KEY is not set. Add it to your .env file "
        '(generate with: python3 -c "import secrets; print(secrets.token_hex(32))").'
    )

app = FastAPI(title="Study Tool")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET_KEY,
    max_age=60 * 60 * 24 * 30,  # 30 days
    same_site="lax",
    https_only=os.environ.get("SESSION_HTTPS_ONLY", "false").lower() == "true",
)


@app.on_event("startup")
def on_startup() -> None:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    init_db()


PARSERS = {
    ".pdf": pdf_parser.extract_text,
    ".docx": docx_parser.extract_text,
}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class SignupRequest(BaseModel):
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


@app.post("/api/auth/signup")
def signup(payload: SignupRequest, request: Request):
    email = auth.normalize_email(payload.email)
    if "@" not in email or not email:
        raise HTTPException(status_code=400, detail="Enter a valid email address.")
    if len(payload.password) < auth.MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400, detail=f"Password must be at least {auth.MIN_PASSWORD_LENGTH} characters."
        )
    if users_db.get_user_by_email(email):
        raise HTTPException(status_code=400, detail="An account with this email already exists.")

    is_first_user = users_db.count_users() == 0
    password_hash = auth.hash_password(payload.password)
    user_id = users_db.create_user(email, password_hash)

    if is_first_user:
        # Preserve any study material created before auth existed by handing
        # it to the very first account — see users.claim_orphaned_data.
        users_db.claim_orphaned_data(user_id)

    request.session["user_id"] = user_id
    return {"id": user_id, "email": email}


@app.post("/api/auth/login")
def login(payload: LoginRequest, request: Request):
    email = auth.normalize_email(payload.email)
    user = users_db.get_user_by_email(email)
    if user is None or not auth.verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    request.session["user_id"] = user["id"]
    return {"id": user["id"], "email": user["email"]}


@app.post("/api/auth/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/auth/me")
def get_me(request: Request):
    user_id = request.session.get("user_id")
    if user_id is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = users_db.get_user_by_id(user_id)
    if user is None:
        request.session.clear()
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"id": user["id"], "email": user["email"]}


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

@app.post("/api/upload")
async def upload_document(
    file: UploadFile = File(...), topic: str = Form(""), user_id: int = Depends(auth.require_user_id)
):
    suffix = Path(file.filename).suffix.lower()
    if suffix not in PARSERS:
        raise HTTPException(status_code=400, detail="Only PDF and DOCX files are supported.")

    # Prefix with user_id to avoid two users' same-named uploads colliding on disk.
    dest_path = UPLOADS_DIR / f"{user_id}_{file.filename}"
    contents = await file.read()
    dest_path.write_bytes(contents)

    try:
        raw_text = PARSERS[suffix](str(dest_path))
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Failed to extract text: {exc}")

    uploaded_at = datetime.now(timezone.utc).isoformat()

    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO documents (filename, topic, raw_text, uploaded_at, user_id) VALUES (?, ?, ?, ?, ?)",
            (file.filename, topic, raw_text, uploaded_at, user_id),
        )
        conn.commit()
        document_id = cursor.lastrowid
    finally:
        conn.close()

    return {
        "id": document_id,
        "filename": file.filename,
        "topic": topic,
        "uploaded_at": uploaded_at,
        "text_length": len(raw_text),
        "text_preview": raw_text[:1000],
    }


@app.get("/api/documents")
def list_documents(user_id: int = Depends(auth.require_user_id)):
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, filename, topic, uploaded_at, length(raw_text) AS text_length "
            "FROM documents WHERE user_id = ? ORDER BY uploaded_at DESC",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


@app.get("/api/documents/{document_id}")
def get_document(document_id: int, user_id: int = Depends(auth.require_user_id)):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id, filename, topic, raw_text, uploaded_at FROM documents WHERE id = ? AND user_id = ?",
            (document_id, user_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    return dict(row)


@app.delete("/api/documents/{document_id}")
def delete_document(document_id: int, user_id: int = Depends(auth.require_user_id)):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT filename FROM documents WHERE id = ? AND user_id = ?", (document_id, user_id)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Document not found.")

        question_ids = [
            r["id"] for r in conn.execute(
                "SELECT id FROM questions WHERE document_id = ? AND user_id = ?", (document_id, user_id)
            ).fetchall()
        ]
        if question_ids:
            placeholders = ",".join("?" for _ in question_ids)
            # Attempts reference questions with foreign_keys enforcement on, so
            # they must go first or the question deletes below would fail.
            conn.execute(f"DELETE FROM attempts WHERE question_id IN ({placeholders})", question_ids)

        conn.execute("DELETE FROM questions WHERE document_id = ? AND user_id = ?", (document_id, user_id))
        conn.execute("DELETE FROM study_guides WHERE document_id = ? AND user_id = ?", (document_id, user_id))
        conn.execute("DELETE FROM documents WHERE id = ? AND user_id = ?", (document_id, user_id))
        conn.commit()

        uploaded_file = UPLOADS_DIR / row["filename"]
        uploaded_file.unlink(missing_ok=True)
    finally:
        conn.close()

    return {"deleted": document_id}


@app.post("/api/documents/{document_id}/study_guide")
def create_study_guide(document_id: int, user_id: int = Depends(auth.require_user_id)):
    try:
        guide = study_guide.generate_and_save_study_guide(document_id, user_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return guide


@app.get("/api/documents/{document_id}/study_guide")
def get_study_guide(document_id: int, user_id: int = Depends(auth.require_user_id)):
    guide = study_guide.get_latest_study_guide(document_id, user_id)
    if guide is None:
        raise HTTPException(status_code=404, detail="No study guide generated yet.")
    return guide


@app.post("/api/documents/{document_id}/questions")
def create_question_bank(document_id: int, user_id: int = Depends(auth.require_user_id)):
    try:
        bank = question_bank.generate_and_save_question_bank(document_id, user_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return bank


@app.get("/api/documents/{document_id}/questions")
def list_question_bank(document_id: int, user_id: int = Depends(auth.require_user_id)):
    bank = question_bank.get_question_bank(document_id, user_id)
    if not bank["calibration"] and not bank["concepts"]:
        raise HTTPException(status_code=404, detail="No question bank generated yet.")
    return bank


# ---------------------------------------------------------------------------
# Quiz sessions
# ---------------------------------------------------------------------------

@app.get("/api/topics")
def get_topics(user_id: int = Depends(auth.require_user_id)):
    return session_runner.list_topics(user_id)


class StartSessionRequest(BaseModel):
    topics: list[str]


class AnswerRequest(BaseModel):
    question_id: int
    answer: str


@app.post("/api/sessions")
def create_session(payload: StartSessionRequest, user_id: int = Depends(auth.require_user_id)):
    if not payload.topics:
        raise HTTPException(status_code=400, detail="Select at least one topic.")
    return session_runner.start_session(payload.topics, user_id)


@app.get("/api/sessions/{session_id}/current")
def get_session_current(session_id: int, user_id: int = Depends(auth.require_user_id)):
    try:
        return session_runner.get_current(session_id, user_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/api/sessions/{session_id}/answer")
def answer_session(session_id: int, payload: AnswerRequest, user_id: int = Depends(auth.require_user_id)):
    try:
        return session_runner.submit_answer(session_id, user_id, payload.question_id, payload.answer)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/sessions")
def get_sessions(user_id: int = Depends(auth.require_user_id)):
    return history.list_sessions(user_id)


@app.get("/api/topics/performance")
def get_topics_performance(user_id: int = Depends(auth.require_user_id)):
    return history.topic_performance(user_id)


# ---------------------------------------------------------------------------
# Static assets and pages
#
# Only specific known assets are served (not a blanket static mount over the
# whole frontend directory) so that an unauthenticated request can't fetch a
# page's raw HTML/JS by hitting an alternate /static/*.html path and bypass
# the login redirect below.
# ---------------------------------------------------------------------------

@app.get("/static/style.css")
def serve_stylesheet():
    return FileResponse(FRONTEND_DIR / "style.css")


@app.get("/static/nav.js")
def serve_nav_js():
    return FileResponse(FRONTEND_DIR / "nav.js")


def _serve_protected_page(request: Request, filename: str):
    if request.session.get("user_id") is None:
        return RedirectResponse(url="/login.html")
    return FileResponse(FRONTEND_DIR / filename)


@app.get("/")
def serve_index(request: Request):
    return _serve_protected_page(request, "index.html")


@app.get("/study_guide.html")
def serve_study_guide_page(request: Request):
    return _serve_protected_page(request, "study_guide.html")


@app.get("/quiz.html")
def serve_quiz_page(request: Request):
    return _serve_protected_page(request, "quiz.html")


@app.get("/history.html")
def serve_history_page(request: Request):
    return _serve_protected_page(request, "history.html")


@app.get("/login.html")
def serve_login_page(request: Request):
    if request.session.get("user_id") is not None:
        return RedirectResponse(url="/")
    return FileResponse(FRONTEND_DIR / "login.html")


@app.get("/signup.html")
def serve_signup_page(request: Request):
    if request.session.get("user_id") is not None:
        return RedirectResponse(url="/")
    return FileResponse(FRONTEND_DIR / "signup.html")
