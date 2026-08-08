#!/usr/bin/env python3
"""
Runs the TradingLoop in a background thread and serves the dashboard
(FastAPI + static frontend) in the foreground.

Usage: python scripts/run_dashboard.py [--host 127.0.0.1] [--port 8000]

Trades, order status changes, candidate state transitions, Momentum
Ignition Score snapshots, and momentum events (traded and non-traded, with
forward-looking outcome windows) are all persisted to DATABASE_URL as they
happen (see db/repository.py) so the dashboard's historical views -- and
future offline analysis of the MIS formula -- survive a restart. Live
candidate/position/risk-event state is read directly off the running
TradingLoop/RiskEngine instead, so it reflects the current process exactly
(see dashboard/app.py).

Note: momentum scores are written on every tick for every actively-watched
candidate (that's the point -- comparing MIS formulas offline needs a dense
history, not just samples at trigger time), so this table can grow quickly
with many candidates and a short poll interval. No throttling is applied
here; add one (e.g. only write every Nth tick per symbol) if row volume
becomes a problem for your database.
"""
from __future__ import annotations

import argparse
import logging
import threading

import uvicorn

from webull_bot.collection.event_recorder import MomentumEventTracker
from webull_bot.config import get_settings
from webull_bot.dashboard.app import create_app
from webull_bot.db.repository import (
    DBBackedEventRecorder,
    record_momentum_score,
    record_order,
    record_scanner_event,
    record_trade,
)
from webull_bot.db.session import create_all, get_session_factory
from webull_bot.main import build_trading_loop

logger = logging.getLogger(__name__)


def _make_trade_persister(session_factory, trading_mode: str):
    def on_trade_closed(trade):
        print(f"TRADE CLOSED: {trade.symbol} pnl={trade.pnl:.2f} ({trade.exit_reason.value})")
        with session_factory() as session:
            try:
                record_trade(session, trade, trading_mode=trading_mode)
                session.commit()
            except Exception:
                session.rollback()
                logger.exception("Failed to persist trade for %s", trade.symbol)

    return on_trade_closed


def _make_order_persister(session_factory, trading_mode: str):
    def on_order_update(order):
        with session_factory() as session:
            try:
                record_order(session, order, trading_mode=trading_mode)
                session.commit()
            except Exception:
                session.rollback()
                logger.exception("Failed to persist order %s", order.client_order_id)

    return on_order_update


def _make_state_transition_persister(session_factory):
    def on_state_transition(symbol, from_state, to_state, timestamp):
        with session_factory() as session:
            try:
                record_scanner_event(
                    session, symbol=symbol, from_state=from_state, to_state=to_state,
                    timestamp=timestamp, reason=f"{from_state.value} -> {to_state.value}",
                )
                session.commit()
            except Exception:
                session.rollback()
                logger.exception("Failed to persist state transition for %s", symbol)

    return on_state_transition


def _make_score_persister(session_factory):
    def on_score_computed(symbol, score):
        with session_factory() as session:
            try:
                record_momentum_score(session, score)
                session.commit()
            except Exception:
                session.rollback()
                logger.exception("Failed to persist momentum score for %s", symbol)

    return on_score_computed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    settings = get_settings()
    create_all(settings)
    session_factory = get_session_factory(settings)
    momentum_event_tracker = MomentumEventTracker(DBBackedEventRecorder(session_factory))

    loop = build_trading_loop(
        settings=settings,
        on_trade_closed=_make_trade_persister(session_factory, settings.trading_mode.value),
        on_order_update=_make_order_persister(session_factory, settings.trading_mode.value),
        on_state_transition=_make_state_transition_persister(session_factory),
        on_score_computed=_make_score_persister(session_factory),
        momentum_event_tracker=momentum_event_tracker,
    )

    stop_flag = threading.Event()
    loop_thread = threading.Thread(
        target=loop.run_forever, kwargs={"stop_flag": stop_flag.is_set}, daemon=True
    )
    loop_thread.start()
    print(f"TradingLoop running in background thread (trading_mode={settings.trading_mode.value}).")

    app = create_app(loop, session_factory, settings.trading_mode.value)
    print(f"Dashboard: http://{args.host}:{args.port}")
    try:
        uvicorn.run(app, host=args.host, port=args.port)
    finally:
        stop_flag.set()


if __name__ == "__main__":
    main()
