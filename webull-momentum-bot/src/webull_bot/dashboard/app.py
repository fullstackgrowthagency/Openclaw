"""
FastAPI dashboard: live in-process state (candidates, positions, risk
events -- read directly off a running TradingLoop/RiskEngine, no DB
round-trip) plus historical data (trades, performance) from the database.

`create_app()` takes the TradingLoop and a DB session factory as explicit
arguments rather than importing globals, so tests can pass fakes/an
in-memory SQLite session factory without running a real bot or DB.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware

from ..db.repository import (
    get_momentum_score_component_summary,
    get_momentum_scores,
    get_performance_summary,
    get_recent_trades,
)
from ..runtime.trading_loop import TradingLoop

_STATIC_DIR = Path(__file__).parent / "static"


class _NoCacheMiddleware(BaseHTTPMiddleware):
    """Without this, a phone/mobile browser can end up running a stale
    cached app.js against a freshly-deployed index.html -- confirmed live
    2026-08-09: after adding a Price column, a user's browser kept an old
    cached app.js that only built 6 <td> cells per row against the new
    7-<th> header, silently shifting every column from Price onward left
    by one (Resistance's value appeared under the Price header, the reason
    text under Resistance, the timestamp under Reason, and no data at all
    under Updated) -- with no error, just data quietly under the wrong
    label. This is a small internal dev dashboard, not a high-traffic
    site, so unconditionally disabling caching on every response is a
    simpler and more robust fix than cache-busting query strings or
    fine-grained per-asset cache policy."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response


def _last_transition_reason(notes: str) -> str | None:
    """Candidate.notes accumulates one line per transition, formatted by
    state_machine.py's transition() as "[iso timestamp] -> state: reason".
    Only the most recent line explains the candidate's *current* state (e.g.
    why it's REJECTED) -- the State/Updated columns already show the state
    and timestamp, so this strips the "[ts] -> state:" prefix rather than
    duplicating them in the dashboard's Reason column."""
    if not notes:
        return None
    last_line = notes.rsplit("\n", 1)[-1]
    return last_line.split(": ", 1)[-1] if ": " in last_line else last_line


def create_app(trading_loop: TradingLoop, session_factory: Callable[[], Session], trading_mode: str) -> FastAPI:
    app = FastAPI(title="Webull Momentum Bot Dashboard")
    app.add_middleware(_NoCacheMiddleware)

    @app.get("/api/status")
    def get_status():
        try:
            equity = trading_loop.broker.get_account_equity()
            buying_power = trading_loop.broker.get_buying_power()
        except Exception as exc:
            equity = buying_power = None
            equity_error = str(exc)
        else:
            equity_error = None
        return {
            "trading_mode": trading_mode,
            "equity": equity,
            "buying_power": buying_power,
            "equity_error": equity_error,
            "candidate_count": len(trading_loop.get_candidates()),
            "open_position_count": len(trading_loop.get_open_positions()),
            "kill_switch_active": trading_loop.risk_engine.kill_switch_active,
        }

    @app.get("/api/candidates")
    def get_candidates():
        rows = []
        for candidate in trading_loop.get_candidates().values():
            last_reason = _last_transition_reason(candidate.notes)
            rows.append({
                "symbol": candidate.symbol,
                "state": candidate.state.value,
                "score": candidate.latest_score.score if candidate.latest_score else None,
                "price": candidate.last_price,
                "resistance_level": candidate.resistance_level,
                "discovered_at": candidate.discovered_at.isoformat(),
                "last_updated_at": candidate.last_updated_at.isoformat(),
                "reason": last_reason,
            })
        rows.sort(key=lambda r: r["score"] or 0, reverse=True)
        return rows

    @app.get("/api/positions")
    def get_positions():
        rows = []
        for symbol, position in trading_loop.get_open_positions().items():
            current_price = None
            unrealized_pnl = None
            try:
                current_price = trading_loop.broker.get_snapshot(symbol).last_price
                unrealized_pnl = (current_price - position.avg_entry_price) * position.quantity
            except Exception:
                pass
            rows.append({
                "symbol": symbol,
                "side": position.side.value,
                "quantity": position.quantity,
                "avg_entry_price": position.avg_entry_price,
                "current_price": current_price,
                "unrealized_pnl": unrealized_pnl,
                "stop_price": position.stop_price,
                "target_price": position.target_price,
                "max_favorable_excursion": position.max_favorable_excursion,
                "max_adverse_excursion": position.max_adverse_excursion,
                "opened_at": position.opened_at.isoformat(),
                "strategy_name": position.strategy_name,
            })
        return rows

    @app.get("/api/risk-events")
    def get_risk_events(limit: int = 50):
        events = trading_loop.risk_engine.events[-limit:]
        return [
            {
                "event_type": e.event_type,
                "symbol": e.symbol,
                "timestamp": e.timestamp.isoformat(),
                "reason": e.reason,
            }
            for e in reversed(events)
        ]

    @app.get("/api/trades")
    def get_trades(limit: int = 100):
        with session_factory() as session:
            rows = get_recent_trades(session, limit=limit)
            return [
                {
                    "symbol": t.symbol,
                    "strategy_name": t.strategy_name,
                    "side": t.side,
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "quantity": t.quantity,
                    "opened_at": t.opened_at.isoformat(),
                    "closed_at": t.closed_at.isoformat(),
                    "exit_reason": t.exit_reason,
                    "pnl": t.pnl,
                    "pnl_pct": t.pnl_pct,
                }
                for t in rows
            ]

    @app.get("/api/performance")
    def get_performance():
        with session_factory() as session:
            return get_performance_summary(session)

    @app.get("/api/score-breakdown")
    def get_score_breakdown():
        """Sanity-check aid for the MIS weighting: which components are
        actually driving scores up in practice, averaged over recent
        history for the current weights_version -- see
        db/repository.py's get_momentum_score_component_summary."""
        with session_factory() as session:
            return get_momentum_score_component_summary(session)

    @app.get("/api/score-history")
    def get_score_history(symbol: str, limit: int = 50):
        with session_factory() as session:
            rows = get_momentum_scores(session, symbol=symbol.upper(), limit=limit)
            return [
                {
                    "timestamp": r.timestamp.isoformat(),
                    "score": r.score,
                    "weights_version": r.weights_version,
                    "components": r.components,
                }
                for r in rows
            ]

    if _STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")

    return app
