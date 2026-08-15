"""POST /api/auth/signup, /login, /logout and GET /api/auth/me.

No email verification and no self-serve "forgot password" flow in v1 --
signup creates the account directly, and password resets for the small
early user base are handled manually (VPS shell access to update
password_hash) rather than building SMTP/SES delivery this codebase has
no other use for yet. Revisit if/when the user base grows past what that
can reasonably support.

Session state itself is Starlette's SessionMiddleware (signed httpOnly
cookie) -- signup/login write request.session["user_id"], logout clears
it. build_auth_router takes session_factory as an explicit argument
(same DI pattern as dashboard/app.py's create_app) so it's testable
against an in-memory SQLite database."""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from ..db.models import User
from ..db.repository import get_or_create_default_bot
from .security import hash_password, verify_password

_MIN_PASSWORD_LENGTH = 8


def _normalize_email(email: str) -> str:
    email = email.strip().lower()
    # Deliberately loose validation (contains "@", something on both
    # sides) rather than a full RFC 5322 parse -- this app has no email
    # deliverability to protect (no verification/reset emails are sent in
    # v1, see this module's docstring), so the only real requirement is a
    # sane, unique-enough identifier, not a fully-validated address.
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise ValueError("Enter a valid email address.")
    return email


class SignupRequest(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def _valid_email(cls, v: str) -> str:
        return _normalize_email(v)


class LoginRequest(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def _valid_email(cls, v: str) -> str:
        return _normalize_email(v)


def build_auth_router(session_factory: Callable[[], Session]) -> APIRouter:
    router = APIRouter(prefix="/api/auth", tags=["auth"])

    @router.post("/signup")
    def signup(body: SignupRequest, request: Request):
        if len(body.password) < _MIN_PASSWORD_LENGTH:
            raise HTTPException(
                status_code=422, detail=f"Password must be at least {_MIN_PASSWORD_LENGTH} characters."
            )
        with session_factory() as session:
            existing = session.query(User).filter(User.email == body.email).one_or_none()
            if existing is not None:
                raise HTTPException(status_code=409, detail="An account with this email already exists.")
            user = User(email=body.email, password_hash=hash_password(body.password))
            session.add(user)
            session.flush()
            # Every user starts with their "Day Trading Quant" bot already
            # registered (2026-08-15 multi-bot framework) -- see
            # db/repository.py's get_or_create_default_bot and its
            # module-level constants for the exact name/slug/kind.
            get_or_create_default_bot(session, user.id)
            session.commit()
            session.refresh(user)
            user_id = user.id
        request.session["user_id"] = user_id
        return {"id": user_id, "email": body.email}

    @router.post("/login")
    def login(body: LoginRequest, request: Request):
        with session_factory() as session:
            user = session.query(User).filter(User.email == body.email).one_or_none()
            if user is None or not user.is_active or not verify_password(body.password, user.password_hash):
                raise HTTPException(status_code=401, detail="Incorrect email or password.")
            user_id, user_email = user.id, user.email
        request.session["user_id"] = user_id
        return {"id": user_id, "email": user_email}

    @router.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return {"logged_out": True}

    @router.get("/me")
    def me(request: Request):
        user_id = request.session.get("user_id")
        if user_id is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        with session_factory() as session:
            user = session.get(User, user_id)
            if user is None or not user.is_active:
                raise HTTPException(status_code=401, detail="Not authenticated")
            return {"id": user.id, "email": user.email}

    return router
