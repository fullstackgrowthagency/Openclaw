"""
Thin FastAPI app factory for locally serving the pairing routes ahead of
Phase 6's real dashboard app. Phase 6 absorbs `build_pairing_router`
directly into `dashboard/app.py` and this file goes away -- a deletion,
not a rework.

Run locally: `uvicorn "fx_bot.brokers.local_connector.pairing.app:create_pairing_app" --factory`
"""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI

from ....config import Settings, get_settings
from .routes import build_pairing_router
from .store import PairingStore


def create_pairing_app(*, settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or get_settings()
    store = PairingStore(settings.pairing_db_path)
    app = FastAPI(title="fx-bot pairing (Phase 5c)")
    app.include_router(build_pairing_router(store, settings=settings))
    return app
