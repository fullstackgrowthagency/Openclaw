"""GET/POST/DELETE /api/broker-credential, POST /api/broker-credential/test,
and POST /api/broker-credential/live-trading -- lets a logged-in user
connect their OWN Webull API key from the dashboard (see db/models.py's
BrokerCredential) instead of sharing the operator's. This is the
mechanism that gives each user their own independent Webull rate-limit
budget once LoopRegistry (see runtime/loop_registry.py, added in the same
multi-tenant conversion) starts one TradingLoop per verified user.

Every route here requires a valid session (get_current_user) and only
ever reads/writes the CALLING user's own row -- there is no path to read
or modify another user's credentials.

Encrypted at rest via auth/crypto.py (Fernet, keyed by
CREDENTIAL_ENCRYPTION_KEY) -- the raw app_key/app_secret/account_id are
never returned to the client once stored, only a masked last-4-chars
preview.

Live trading is self-serve here (2026-08-15 design decision -- see
docs/ARCHITECTURE.md's "Multi-tenant auth" section): once a user's
credentials are verified, they can flip live_trading_enabled themselves
from the dashboard, no operator approval step. `confirm=True` on that
request is this feature's equivalent of config.py's
LIVE_TRADING_CONFIRMATION phrase -- a deliberate second, explicit
"yes I mean it" signal distinct from the enabled flag itself, required
only when turning live trading ON, not off.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..brokers.webull.client import WebullBrokerClient
from ..config import Settings, TradingMode, WebullCredentials
from ..db.models import BrokerCredential, User
from ..runtime.loop_registry import LoopRegistry
from .crypto import CredentialEncryptionError, decrypt_secret, encrypt_secret
from .dependencies import build_get_current_user

_TRADING_MODES = {m.value for m in TradingMode}


def build_settings_for_user(cred: BrokerCredential, settings: Settings) -> Settings:
    """Constructs the per-user Settings object LoopRegistry passes to
    build_trading_loop for this user (see runtime/loop_registry.py,
    scripts/run_dashboard.py, and this module's own start/stop calls
    below) -- webull credentials/trading_mode come from this user's own
    verified BrokerCredential; everything else (float-data provider keys,
    the deployment-wide live-trading gate, DB URL, ...) is shared from the
    process-wide `settings` (see docs/ARCHITECTURE.md's "Multi-tenant
    auth" section for why those specifically stay shared, not per-user).

    live_trading_enabled is the AND of this user's own self-serve toggle
    and the operator's deployment-wide LIVE_TRADING_ENABLED -- a user
    can only ever reach real live trading if BOTH are true, same
    two-layer gate _verify's docstring above describes for the
    verify-a-key path."""
    webull = WebullCredentials(
        app_key=decrypt_secret(cred.app_key_encrypted, settings),
        app_secret=decrypt_secret(cred.app_secret_encrypted, settings),
        account_id=decrypt_secret(cred.account_id_encrypted, settings),
        base_url=cred.base_url,
    )
    return Settings(
        trading_mode=TradingMode(cred.trading_mode),
        environment=settings.environment,
        live_trading_enabled=cred.live_trading_enabled and settings.live_trading_enabled,
        live_trading_confirmation=settings.live_trading_confirmation,
        webull=webull,
        massive=settings.massive,
        fmp=settings.fmp,
        database=settings.database,
        float_cache_dir=settings.float_cache_dir,
        float_cache_ttl_hours=settings.float_cache_ttl_hours,
        enable_yfinance_fallback=settings.enable_yfinance_fallback,
        webull_support_trading_session=settings.webull_support_trading_session,
        session_secret_key=settings.session_secret_key,
        credential_encryption_key=settings.credential_encryption_key,
    )


class BrokerCredentialUpdate(BaseModel):
    app_key: str
    app_secret: str
    account_id: str
    base_url: str
    trading_mode: str = "sandbox"


class LiveTradingToggle(BaseModel):
    enabled: bool
    confirm: bool = False


def _mask(value: str) -> str:
    return f"...{value[-4:]}" if len(value) >= 4 else "****"


def _verify(app_key: str, app_secret: str, account_id: str, base_url: str, trading_mode: str) -> tuple[bool, str | None]:
    """Attempts a real, cheap Webull call (account equity) with these exact
    credentials -- the only way to actually know a submitted key/secret
    pair works, distinct from just checking the fields are non-empty.
    Builds a throwaway Settings/WebullBrokerClient rather than touching
    the process-wide get_settings() singleton, so this never has any
    effect on any other user's (or the operator's) connection.

    trading_mode="live" will always fail here, by design:
    WebullBrokerClient.__init__ calls Settings.require_non_live_or_authorized(),
    which raises unless live_trading_enabled AND the exact confirmation
    phrase are set on the Settings object -- and this throwaway one never
    sets either. That's the process-wide deployment-level gate (config.py)
    still doing its job underneath this per-user, self-serve
    live_trading_enabled column (see this module's docstring) -- a user
    can only ever reach real live trading if the OPERATOR has also
    enabled it for the whole deployment via LIVE_TRADING_ENABLED/
    LIVE_TRADING_CONFIRMATION. Connect with trading_mode="sandbox" or
    "paper" to verify a key; live-order routing is wired in Phase D's
    per-user Settings construction, not exercised by this verify call."""
    trial_settings = Settings(
        trading_mode=TradingMode(trading_mode),
        webull=WebullCredentials(app_key=app_key, app_secret=app_secret, account_id=account_id, base_url=base_url),
    )
    client = None
    try:
        client = WebullBrokerClient(trial_settings)
        client.connect()
        client.get_account_equity()
        return True, None
    except Exception as exc:
        return False, str(exc)
    finally:
        if client is not None:
            client.disconnect()


def _credential_response(cred: BrokerCredential, settings: Settings) -> dict:
    if cred is None:
        return {"connected": False}
    try:
        masked_app_key = _mask(decrypt_secret(cred.app_key_encrypted, settings))
    except CredentialEncryptionError:
        masked_app_key = None  # CREDENTIAL_ENCRYPTION_KEY changed/missing -- see crypto.py
    return {
        "connected": True,
        "broker": cred.broker,
        "masked_app_key": masked_app_key,
        "base_url": cred.base_url,
        "trading_mode": cred.trading_mode,
        "live_trading_enabled": cred.live_trading_enabled,
        "last_verified_at": cred.last_verified_at.isoformat() if cred.last_verified_at else None,
        "last_verify_error": cred.last_verify_error,
    }


def build_broker_credential_router(
    session_factory: Callable[[], Session], settings: Settings, loop_registry: Optional[LoopRegistry] = None,
) -> APIRouter:
    """`loop_registry`: when provided (see scripts/run_dashboard.py), every
    mutation below that changes what a user's TradingLoop should be
    running with -- a newly-verified connection, a live-trading toggle,
    or a disconnect -- restarts (stop then start, or just stop) that
    user's loop so the change takes effect immediately, not on next
    process restart. None in tests/contexts that only exercise storage,
    not the live-loop side effect."""
    router = APIRouter(prefix="/api/broker-credential", tags=["broker-credential"])
    get_current_user = build_get_current_user(session_factory)

    def _restart_loop_for(user_id: int, cred: BrokerCredential) -> None:
        if loop_registry is None:
            return
        loop_registry.stop_for_user(user_id)
        if cred.last_verified_at is not None:
            loop_registry.start_for_user(user_id, build_settings_for_user(cred, settings))

    @router.get("")
    def get_credential(user: User = Depends(get_current_user)):
        with session_factory() as session:
            cred = session.query(BrokerCredential).filter(BrokerCredential.user_id == user.id).one_or_none()
            return _credential_response(cred, settings)

    @router.post("")
    def set_credential(body: BrokerCredentialUpdate, user: User = Depends(get_current_user)):
        if body.trading_mode not in _TRADING_MODES:
            raise HTTPException(status_code=422, detail=f"trading_mode must be one of {sorted(_TRADING_MODES)}.")
        if not (body.app_key and body.app_secret and body.account_id and body.base_url):
            raise HTTPException(status_code=422, detail="app_key, app_secret, account_id, and base_url are all required.")

        verified, error = _verify(body.app_key, body.app_secret, body.account_id, body.base_url, body.trading_mode)

        with session_factory() as session:
            cred = session.query(BrokerCredential).filter(BrokerCredential.user_id == user.id).one_or_none()
            if cred is None:
                cred = BrokerCredential(user_id=user.id)
                session.add(cred)
            cred.app_key_encrypted = encrypt_secret(body.app_key, settings)
            cred.app_secret_encrypted = encrypt_secret(body.app_secret, settings)
            cred.account_id_encrypted = encrypt_secret(body.account_id, settings)
            cred.base_url = body.base_url
            cred.trading_mode = body.trading_mode
            cred.updated_at = datetime.utcnow()
            if verified:
                cred.last_verified_at = datetime.utcnow()
                cred.last_verify_error = None
            else:
                # Stored anyway (not discarded) so /test can retry against
                # it without the user re-typing their secret -- but
                # last_verified_at is left untouched (None on first save)
                # and the response below is a 400, so nothing treats this
                # as a working connection.
                cred.last_verify_error = error
                if cred.last_verified_at is None:
                    cred.live_trading_enabled = False
            session.commit()
            session.refresh(cred)
            response = _credential_response(cred, settings)
            _restart_loop_for(user.id, cred)

        if not verified:
            raise HTTPException(status_code=400, detail=f"Could not verify these credentials: {error}")
        return response

    @router.post("/test")
    def test_credential(user: User = Depends(get_current_user)):
        with session_factory() as session:
            cred = session.query(BrokerCredential).filter(BrokerCredential.user_id == user.id).one_or_none()
            if cred is None:
                raise HTTPException(status_code=404, detail="No broker credential connected yet.")
            app_key = decrypt_secret(cred.app_key_encrypted, settings)
            app_secret = decrypt_secret(cred.app_secret_encrypted, settings)
            account_id = decrypt_secret(cred.account_id_encrypted, settings)
            base_url, trading_mode = cred.base_url, cred.trading_mode

        verified, error = _verify(app_key, app_secret, account_id, base_url, trading_mode)

        with session_factory() as session:
            cred = session.query(BrokerCredential).filter(BrokerCredential.user_id == user.id).one_or_none()
            if verified:
                cred.last_verified_at = datetime.utcnow()
                cred.last_verify_error = None
            else:
                cred.last_verify_error = error
            session.commit()
            session.refresh(cred)
            response = _credential_response(cred, settings)
            _restart_loop_for(user.id, cred)

        if not verified:
            raise HTTPException(status_code=400, detail=f"Could not verify these credentials: {error}")
        return response

    @router.delete("")
    def delete_credential(user: User = Depends(get_current_user)):
        with session_factory() as session:
            cred = session.query(BrokerCredential).filter(BrokerCredential.user_id == user.id).one_or_none()
            if cred is not None:
                session.delete(cred)
                session.commit()
        if loop_registry is not None:
            loop_registry.stop_for_user(user.id)
        return {"connected": False}

    @router.post("/live-trading")
    def set_live_trading(body: LiveTradingToggle, user: User = Depends(get_current_user)):
        with session_factory() as session:
            cred = session.query(BrokerCredential).filter(BrokerCredential.user_id == user.id).one_or_none()
            if cred is None:
                raise HTTPException(status_code=404, detail="Connect a broker account before enabling live trading.")
            if body.enabled:
                if cred.last_verified_at is None:
                    raise HTTPException(status_code=422, detail="Verify your broker connection before enabling live trading.")
                if not body.confirm:
                    raise HTTPException(
                        status_code=422,
                        detail="Set confirm=true to enable live trading -- this routes real orders to a real account.",
                    )
            cred.live_trading_enabled = body.enabled
            cred.updated_at = datetime.utcnow()
            session.commit()
            session.refresh(cred)
            response = _credential_response(cred, settings)
            _restart_loop_for(user.id, cred)
            return response

    return router
