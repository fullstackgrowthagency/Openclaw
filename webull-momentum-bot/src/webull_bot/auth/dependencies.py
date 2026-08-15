"""FastAPI dependency for resolving the logged-in User off the current
request's session cookie. Session storage itself is Starlette's own
SessionMiddleware (a signed httpOnly cookie, added in dashboard/app.py's
create_app) -- this module only reads request.session["user_id"] and
looks up the row, it doesn't manage the cookie itself.

Factory-based (build_get_current_user(session_factory), not a bare
module-level dependency) for the same reason create_app takes
session_factory as an explicit argument rather than importing a global:
tests need to inject an in-memory SQLite session factory without a real
database."""
from __future__ import annotations

from typing import Callable

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from ..db.models import User

_NOT_AUTHENTICATED = HTTPException(status_code=401, detail="Not authenticated")


def build_get_current_user(session_factory: Callable[[], Session]) -> Callable[[Request], User]:
    def get_current_user(request: Request) -> User:
        user_id = request.session.get("user_id")
        if user_id is None:
            raise _NOT_AUTHENTICATED
        with session_factory() as session:
            user = session.get(User, user_id)
            if user is None or not user.is_active:
                raise _NOT_AUTHENTICATED
            session.expunge(user)  # detach so it's safe to read after the session closes below
            return user

    return get_current_user
