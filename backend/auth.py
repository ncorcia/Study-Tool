"""Password hashing and session-based authentication."""

from fastapi import HTTPException, Request
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

MIN_PASSWORD_LENGTH = 8


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def require_user_id(request: Request) -> int:
    """FastAPI dependency: the logged-in user's id, or 401 if not logged in."""
    user_id = request.session.get("user_id")
    if user_id is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id
