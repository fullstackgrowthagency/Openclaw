#!/usr/bin/env python3
"""
Serves the dashboard (FastAPI + static frontend) in the foreground, with
one TradingLoop running per account in a background thread each.

Usage: python scripts/run_dashboard.py [--host 127.0.0.1] [--port 8000]

Two modes, chosen automatically by whether SESSION_SECRET_KEY is set
(2026-08-15 multi-tenant conversion -- see docs/ARCHITECTURE.md's
"Multi-tenant auth" section):

  - Multi-tenant (SESSION_SECRET_KEY set): boots a LoopRegistry
    (runtime/loop_registry.py) with one TradingLoop per user who has a
    verified BrokerCredential (see auth/broker_credential_routes.py) --
    the dashboard's per-user login/signup/broker-connection UI is what
    populates that table. Every /api/* endpoint then reads from whichever
    user is logged in for that request (dashboard/app.py's _resolve_loop).
  - Single-tenant/legacy (SESSION_SECRET_KEY unset): boots exactly one
    TradingLoop from this process's own .env credentials, exactly as
    before multi-tenant auth existed -- no login step, one shared
    dashboard. This is what an existing pre-multi-tenant deployment keeps
    doing unchanged until an operator opts in.

Trades, order status changes, candidate state transitions, Momentum
Ignition Score snapshots, and momentum events (traded and non-traded, with
forward-looking outcome windows) are all persisted to DATABASE_URL as they
happen (see db/repository.py), tagged with user_id in multi-tenant mode
(NULL in single-tenant mode) so the dashboard's historical views -- and
future offline analysis of the MIS formula -- survive a restart and stay
scoped to the right account. Live candidate/position/risk-event state is
read directly off the running TradingLoop/RiskEngine instead, so it
reflects the current process exactly (see dashboard/app.py).

Note: momentum scores are written on every tick for every actively-watched
candidate (that's the point -- comparing MIS formulas offline needs a dense
history, not just samples at trigger time), so this table can grow quickly
with many candidates and a short poll interval, now multiplied by however
many users are running concurrently in multi-tenant mode. No throttling is
applied here; add one (e.g. only write every Nth tick per symbol) if row
volume becomes a problem for your database.
"""
from __future__ import annotations

import argparse
import logging
import threading
from typing import Optional

import uvicorn

from webull_bot.auth.broker_credential_routes import build_settings_for_user
from webull_bot.collection.event_recorder import MomentumEventTracker
from webull_bot.config import Settings, get_settings
from webull_bot.dashboard.app import create_app
from webull_bot.db.models import BrokerCredential, User
from webull_bot.db.repository import (
    DBBackedEventRecorder,
    record_momentum_score,
    record_order,
    record_scanner_event,
    record_trade,
)
from webull_bot.db.session import create_all, get_session_factory
from webull_bot.main import build_trading_loop
from webull_bot.runtime.loop_registry import LoopRegistry
from webull_bot.runtime.trading_loop import TradingLoop

logger = logging.getLogger(__name__)


def _make_trade_persister(session_factory, trading_mode: str, user_id: Optional[int] = None):
    def on_trade_closed(trade):
        print(f"TRADE CLOSED: {trade.symbol} pnl={trade.pnl:.2f} ({trade.exit_reason.value})")
        with session_factory() as session:
            try:
                record_trade(session, trade, trading_mode=trading_mode, user_id=user_id)
                session.commit()
            except Exception:
                session.rollback()
                logger.exception("Failed to persist trade for %s", trade.symbol)

    return on_trade_closed


def _make_order_persister(session_factory, trading_mode: str, user_id: Optional[int] = None):
    def on_order_update(order):
        with session_factory() as session:
            try:
                record_order(session, order, trading_mode=trading_mode, user_id=user_id)
                session.commit()
            except Exception:
                session.rollback()
                logger.exception("Failed to persist order %s", order.client_order_id)

    return on_order_update


def _make_state_transition_persister(session_factory, user_id: Optional[int] = None):
    def on_state_transition(symbol, from_state, to_state, timestamp):
        with session_factory() as session:
            try:
                record_scanner_event(
                    session, symbol=symbol, from_state=from_state, to_state=to_state,
                    timestamp=timestamp, reason=f"{from_state.value} -> {to_state.value}", user_id=user_id,
                )
                session.commit()
            except Exception:
                session.rollback()
                logger.exception("Failed to persist state transition for %s", symbol)

    return on_state_transition


def _make_score_persister(session_factory, user_id: Optional[int] = None):
    def on_score_computed(symbol, score):
        with session_factory() as session:
            try:
                record_momentum_score(session, score, user_id=user_id)
                session.commit()
            except Exception:
                session.rollback()
                logger.exception("Failed to persist momentum score for %s", symbol)

    return on_score_computed


def _build_loop_for_user(user_id: Optional[int], user_settings: Settings, session_factory) -> TradingLoop:
    """Same persistence-hook wiring for every account -- single-tenant
    (user_id=None) and every per-user loop in multi-tenant mode alike --
    just parameterized by whose settings/user_id to use. Shared here so
    the single-tenant path below and LoopRegistry's per-user path (see
    _start_multi_tenant) can never drift apart."""
    momentum_event_tracker = MomentumEventTracker(DBBackedEventRecorder(session_factory, user_id=user_id))
    return build_trading_loop(
        settings=user_settings,
        on_trade_closed=_make_trade_persister(session_factory, user_settings.trading_mode.value, user_id),
        on_order_update=_make_order_persister(session_factory, user_settings.trading_mode.value, user_id),
        on_state_transition=_make_state_transition_persister(session_factory, user_id),
        on_score_computed=_make_score_persister(session_factory, user_id),
        momentum_event_tracker=momentum_event_tracker,
    )


def _start_multi_tenant(settings: Settings, session_factory):
    """Boots a LoopRegistry with one TradingLoop already running for every
    user who has a verified BrokerCredential at process startup -- users
    who connect/verify/disconnect a broker account AFTER this point are
    handled live by auth/broker_credential_routes.py's own start/stop
    calls into the same registry (see create_app's loop_registry wiring),
    not by this function again."""
    registry = LoopRegistry(lambda user_id, user_settings: _build_loop_for_user(user_id, user_settings, session_factory))

    with session_factory() as session:
        verified = (
            session.query(User, BrokerCredential)
            .join(BrokerCredential, BrokerCredential.user_id == User.id)
            .filter(User.is_active.is_(True), BrokerCredential.last_verified_at.isnot(None))
            .all()
        )
        for user, cred in verified:
            user_settings = build_settings_for_user(cred, settings)
            registry.start_for_user(user.id, user_settings)

    print(f"Multi-tenant mode: started {len(registry.running_user_ids())} user TradingLoop(s).")
    return registry


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    settings = get_settings()
    create_all(settings)
    session_factory = get_session_factory(settings)

    if settings.session_secret_key:
        registry = _start_multi_tenant(settings, session_factory)
        app = create_app(None, session_factory, settings.trading_mode.value, settings=settings, loop_registry=registry)
        stop_all = registry.stop_all
    else:
        loop = _build_loop_for_user(None, settings, session_factory)
        stop_flag = threading.Event()
        loop_thread = threading.Thread(
            target=loop.run_forever, kwargs={"stop_flag": stop_flag.is_set}, daemon=True
        )
        loop_thread.start()
        print(f"TradingLoop running in background thread (trading_mode={settings.trading_mode.value}).")
        app = create_app(loop, session_factory, settings.trading_mode.value, settings=settings)
        stop_all = stop_flag.set

    print(f"Dashboard: http://{args.host}:{args.port}")
    try:
        uvicorn.run(app, host=args.host, port=args.port)
    finally:
        stop_all()


if __name__ == "__main__":
    main()
