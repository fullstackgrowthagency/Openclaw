#!/usr/bin/env python3
"""
Runs the TradingLoop in a background thread and serves the dashboard
(FastAPI + static frontend) in the foreground.

Usage: python scripts/run_dashboard.py [--host 127.0.0.1] [--port 8000]

Trades and order status changes are persisted to DATABASE_URL as they
happen (see db/repository.py) so the dashboard's historical views survive
a restart; live candidate/position/risk-event state is read directly off
the running TradingLoop/RiskEngine instead, so it reflects the current
process exactly (see dashboard/app.py).
"""
from __future__ import annotations

import argparse
import logging
import threading

import uvicorn

from webull_bot.config import get_settings
from webull_bot.dashboard.app import create_app
from webull_bot.db.repository import record_order, record_trade
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    settings = get_settings()
    create_all(settings)
    session_factory = get_session_factory(settings)

    loop = build_trading_loop(
        settings=settings,
        on_trade_closed=_make_trade_persister(session_factory, settings.trading_mode.value),
        on_order_update=_make_order_persister(session_factory, settings.trading_mode.value),
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
