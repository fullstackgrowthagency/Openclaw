"""
The two pairing HTTP routes. Built as `build_pairing_router(store, *,
settings) -> APIRouter` -- the same dependency-injection-factory shape
webull_bot's own `auth/routes.py` uses (`build_X_router(session_factory,
...)`), so Phase 6's real dashboard app does a plain
`app.include_router(build_pairing_router(...))` with zero rework.

Status-code convention mirrors webull_bot's own auth routes: 404 for a
code that never existed, 400 for a code that existed but is no longer
valid (a verification failure, not a missing resource), 409 for a code
that exists but conflicts with the requested operation (already used).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ....config import Settings
from .exceptions import PairingCodeAlreadyUsed, PairingCodeExpired, PairingCodeNotFound
from .models import PairingCodeResponse, PairRequest, PairResponse
from .store import PairingStore
from .tokens import generate_token, hash_token

# The sole account_id value until Phase 10's real multi-tenancy exists --
# a plain constant, not settings-configurable, so nothing here looks
# configurable that isn't really.
DEFAULT_ACCOUNT_ID = "default-account"


def build_pairing_router(store: PairingStore, *, settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/connector", tags=["pairing"])

    @router.post("/pairing-codes", response_model=PairingCodeResponse, status_code=201)
    def create_pairing_code() -> PairingCodeResponse:
        issued = store.create_pairing_code(DEFAULT_ACCOUNT_ID, ttl_seconds=settings.pairing_code_ttl_seconds)
        return PairingCodeResponse(code=issued.code, expires_at=issued.expires_at)

    @router.post("/pair", response_model=PairResponse)
    def pair(body: PairRequest) -> PairResponse:
        try:
            account_id = store.consume_pairing_code(body.code)
        except PairingCodeNotFound:
            raise HTTPException(status_code=404, detail="Unknown pairing code.")
        except PairingCodeExpired:
            raise HTTPException(status_code=400, detail="Pairing code has expired. Request a new one.")
        except PairingCodeAlreadyUsed:
            raise HTTPException(status_code=409, detail="Pairing code has already been used.")

        token = generate_token()
        store.store_token(account_id, hash_token(token))
        return PairResponse(token=token, account_id=account_id)

    return router
