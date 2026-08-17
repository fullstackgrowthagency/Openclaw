"""
FastAPI dashboard: live in-process state (candidates, positions, risk
events -- read directly off a running TradingLoop/RiskEngine, no DB
round-trip) plus historical data (trades, performance) from the database.

`create_app()` takes the TradingLoop and a DB session factory as explicit
arguments rather than importing globals, so tests can pass fakes/an
in-memory SQLite session factory without running a real bot or DB.

Multi-tenant (2026-08-15, see docs/ARCHITECTURE.md's "Multi-tenant auth"
section): `create_app` also accepts an optional `loop_registry`
(runtime/loop_registry.py) -- when provided, every /api/* endpoint reads
from THIS REQUEST'S authenticated user's own TradingLoop instead of a
single shared one (see `_resolve_loop`), and every DB-backed endpoint
scopes its query to that user's own rows (see `_current_user_id`). Both
require `settings.session_secret_key` to be set; with it unset, the
dashboard behaves exactly as it did before multi-tenant auth existed --
one shared `trading_loop`, no login, no per-user scoping -- which is what
scripts/run_dashboard.py still does for any deployment that hasn't opted
in yet.
"""
from __future__ import annotations

import concurrent.futures
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from ..auth.broker_credential_routes import build_broker_credential_router
from ..auth.dependencies import build_get_current_user
from ..auth.routes import build_auth_router
from ..config import Settings, get_settings
from ..db.models import Bot, User
from ..db.repository import (
    get_momentum_score_component_summary,
    get_momentum_scores,
    get_or_create_default_bot,
    get_performance_summary,
    get_recent_trades,
)
from ..runtime.loop_registry import LoopRegistry
from ..runtime.trading_loop import TradingLoop
from ..scoring.momentum_ignition_score import MISConfig

logger = logging.getLogger(__name__)

# 30 days -- matches this project's plan doc: a signed-cookie session with a
# fixed expiry (re-login after) rather than a sliding window or a DB-backed
# revocable session table, since v1's user base is small enough that
# "logout everywhere" isn't a requirement yet (see auth/routes.py's
# docstring for the same simplicity tradeoff on email/password-reset).
_SESSION_MAX_AGE_SECONDS = 30 * 24 * 60 * 60

_STATIC_DIR = Path(__file__).parent / "static"

# POST /api/scan-symbol's real broker call (BroadScanner.check_symbol_verbose
# -> broker.get_snapshot) is unbatched and shares the exact same
# priority-queued, occasionally-exclusive (place_order/place_oco_bracket)
# webull_limiter that /api/status and /api/positions used to be exposed to
# before their 2026-08-12 fix (see docs/ARCHITECTURE.md's "Dashboard 504s"
# section) -- unlike those two, there's no sensible cached value to read
# instead here (it's an arbitrary, just-typed, possibly-never-seen-before
# symbol), so the fix is a hard wall-clock deadline on the request thread's
# WAIT for the result, not eliminating the broker call. Run in a small,
# dedicated executor (NOT Starlette's own request threadpool) so that if
# the deadline is hit, the scan keeps running to completion in the
# background instead of being abandoned -- scan_and_add_candidate's own
# locked insert (see its docstring) means a slow scan that eventually
# succeeds still adds the candidate for the next /api/candidates poll to
# pick up, even though this particular HTTP response already returned.
# max_workers bounds worst-case thread growth from a user mashing the
# button repeatedly; queued-but-not-yet-started submissions just report
# "still checking" immediately.
_SCAN_SYMBOL_TIMEOUT_SECONDS = 12.0
_scan_symbol_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="scan-symbol"
)


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


# The seven RiskConfig fields adjustable from the dashboard's Settings modal
# -- deliberately a small, curated subset of RiskConfig (not every field),
# matched to what the Settings UI actually exposes. All are percentages
# except min_risk_reward_ratio (a ratio, e.g. 2.0 = "at least 2x reward for
# every 1x risked"), max_simultaneous_positions (a whole-number position
# count, where 0 means unlimited -- see its own validation below), and
# allow_extended_hours_trading (a bool, added 2026-08-12 -- see its own
# validation below and RiskConfig's docstring for what it actually gates).
_ADJUSTABLE_RISK_FIELDS = (
    "stop_loss_pct", "min_risk_reward_ratio", "max_position_size_pct",
    "max_total_risk_pct", "max_daily_loss_pct", "max_simultaneous_positions",
    "allow_extended_hours_trading",
)


class RiskSettingsUpdate(BaseModel):
    stop_loss_pct: Optional[float] = None
    min_risk_reward_ratio: Optional[float] = None
    max_position_size_pct: Optional[float] = None
    max_total_risk_pct: Optional[float] = None
    max_daily_loss_pct: Optional[float] = None
    max_simultaneous_positions: Optional[int] = None
    allow_extended_hours_trading: Optional[bool] = None


# The two PositionManagementConfig fields adjustable from the same Settings
# modal -- a separate config object from RiskConfig above (lives on
# trading_loop.position_manager, not risk_engine), so it gets its own small
# GET/POST pair rather than being folded into /api/risk-settings. Both are
# natively Optional[float] on PositionManagementConfig itself (None means
# "disabled"), but here None means "omitted from this request, leave
# unchanged" -- there's currently no way to use this endpoint to explicitly
# turn a rule off, only to set it to a positive value. Disabling one
# entirely still requires a code-level config change.
_ADJUSTABLE_POSITION_FIELDS = ("trailing_stop_pct", "breakeven_trigger_pct")


class PositionSettingsUpdate(BaseModel):
    trailing_stop_pct: Optional[float] = None
    breakeven_trigger_pct: Optional[float] = None


class KillSwitchUpdate(BaseModel):
    active: bool


def create_app(
    trading_loop: Optional[TradingLoop],
    session_factory: Callable[[], Session],
    trading_mode: str,
    settings: Optional[Settings] = None,
    loop_registry: Optional[LoopRegistry] = None,
) -> FastAPI:
    """`trading_loop`: the single shared loop used in single-tenant/no-auth
    mode (pass None once `loop_registry` is provided -- see below).
    `loop_registry` (2026-08-15 multi-tenant conversion, runtime/
    loop_registry.py): when provided, every `/api/*` endpoint below reads
    from THIS request's authenticated user's own TradingLoop instead of
    the single shared one -- see `_resolve_loop`. Requires
    `settings.session_secret_key` to be set (auth configured); passing a
    `loop_registry` with auth disabled is a caller error, since there
    would be no way to know which user's loop to read."""
    settings = settings or get_settings()
    app = FastAPI(title="Webull Momentum Bot Dashboard")
    app.add_middleware(_NoCacheMiddleware)

    # Auth (2026-08-15 multi-tenant conversion). Guarded on
    # session_secret_key being set rather than failing startup outright:
    # this lets the existing single-tenant deployment keep running
    # unchanged (see docs/ARCHITECTURE.md's "Multi-tenant auth" section)
    # until an operator opts in by setting SESSION_SECRET_KEY, at which
    # point signup/login become live AND every /api/* endpoint below
    # starts requiring a valid session (see _current_user).
    _get_current_user = None
    if settings.session_secret_key:
        app.add_middleware(SessionMiddleware, secret_key=settings.session_secret_key, max_age=_SESSION_MAX_AGE_SECONDS)
        app.include_router(build_auth_router(session_factory))
        app.include_router(build_broker_credential_router(session_factory, settings, loop_registry))
        _get_current_user = build_get_current_user(session_factory)
    else:
        if loop_registry is not None:
            raise RuntimeError("loop_registry requires settings.session_secret_key to be set (auth configured).")
        logger.warning(
            "SESSION_SECRET_KEY is not set -- dashboard auth (signup/login) is disabled; "
            "the dashboard runs exactly as it did before multi-tenant auth was added."
        )

    def _current_user(request: Request) -> Optional[User]:
        """None when auth isn't configured at all (single-tenant/no-auth
        mode -- every endpoint below then reads/writes user_id=NULL,
        matching every row written before this column existed). Raises
        401 (via get_current_user) when auth IS configured and this
        request has no valid session -- every /api/* endpoint becomes
        login-gated the moment an operator sets SESSION_SECRET_KEY,
        without each endpoint needing its own Depends(...)."""
        if _get_current_user is None:
            return None
        return _get_current_user(request)

    def _current_user_id(request: Request) -> Optional[int]:
        user = _current_user(request)
        return user.id if user else None

    def _current_bot_id(request: Request) -> Optional[int]:
        """This request's user's default ("Day Trading Quant") bot id
        (2026-08-15 multi-bot framework) -- None in single-tenant/no-auth
        mode, matching _current_user_id's own None there, so every
        DB-backed endpoint below stays scoped to bot_id=NULL exactly like
        it already scopes to user_id=NULL. get_or_create_default_bot is a
        cheap indexed SELECT in the normal case (every real user already
        has this row from signup or the 0003 migration's backfill) --
        the create branch only matters as a safety net."""
        user_id = _current_user_id(request)
        if user_id is None:
            return None
        with session_factory() as session:
            bot = get_or_create_default_bot(session, user_id)
            session.commit()
            return bot.id

    def _resolve_loop(request: Request) -> TradingLoop:
        """The TradingLoop this request's data should come from: the
        single shared `trading_loop` in single-tenant/no-auth mode, or
        (once `loop_registry` is wired in) this request's own
        authenticated user's own loop. 404s -- not a blank/someone-else's
        loop -- if that user hasn't connected+verified a broker account
        yet, since LoopRegistry only ever has an entry for a verified
        account (see auth/broker_credential_routes.py)."""
        user = _current_user(request)
        if loop_registry is None:
            return trading_loop
        loop = loop_registry.get(user.id if user else None)
        if loop is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    "No trading loop is running for your account yet -- connect and verify a broker "
                    "account first (see Broker Account in the header)."
                ),
            )
        return loop

    @app.get("/api/status")
    def get_status(request: Request):
        loop = _resolve_loop(request)
        # Reads TradingLoop's own periodically-refreshed cache
        # (get_account_summary) instead of calling the broker live on
        # every request -- see that method's docstring for the 504
        # incident (2026-08-12) this fixes: a live call here shared the
        # same rate-limiter queue as order placement, including its
        # exclusive() hold during a real order submission.
        account_summary = loop.get_account_summary()
        return {
            "trading_mode": trading_mode,
            "equity": account_summary["equity"],
            "buying_power": account_summary["buying_power"],
            "equity_error": account_summary["equity_error"],
            "candidate_count": len(loop.get_candidates()),
            "open_position_count": len(loop.get_open_positions()),
            "kill_switch_active": loop.risk_engine.kill_switch_active,
        }

    @app.get("/api/candidates")
    def get_candidates(request: Request):
        loop = _resolve_loop(request)
        rows = []
        for candidate in loop.get_candidates().values():
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
                # Raw per-component sub-scores from this candidate's most
                # recent live tick -- lets the dashboard show this exact
                # candidate's actual score breakdown (see /api/mis-weights
                # for the weights to pair them with) rather than only a
                # historical database average across all candidates. None
                # until CandidateWatcher.update() has ticked this candidate
                # at least once.
                "components": asdict(candidate.latest_score.components) if candidate.latest_score else None,
                "score_weights_version": candidate.latest_score.weights_version if candidate.latest_score else None,
            })
        rows.sort(key=lambda r: r["score"] or 0, reverse=True)
        return rows

    @app.get("/api/mis-weights")
    def get_mis_weights():
        """The currently active (normalized) MIS weights, straight from
        scoring/weights.yaml -- independent of any DB history, so it's
        available even with an empty database (e.g. right after a
        restart). Pairs with /api/candidates' `components` field to render
        a specific candidate's live score breakdown."""
        config = MISConfig.load()
        return {"weights_version": config.version, "weights": config.weights}

    @app.get("/api/risk-settings")
    def get_risk_settings(request: Request):
        """Live values of the RiskConfig fields the dashboard's Settings modal
        exposes -- read straight off the running RiskEngine.config, so this
        always reflects what's actually gating trades right now (including
        any change made through POST /api/risk-settings below), not a static
        default."""
        config = _resolve_loop(request).risk_engine.config
        return {field: getattr(config, field) for field in _ADJUSTABLE_RISK_FIELDS}

    @app.post("/api/risk-settings")
    def update_risk_settings(update: RiskSettingsUpdate, request: Request):
        """Mutates the live RiskEngine.config in place -- takes effect on the
        very next Signal it evaluates, no restart needed. Only fields present
        (non-None) in the request body are changed; omitted fields keep their
        current value. Five of the six fields are percentages/ratios that
        must be positive, and the four percentage fields must not exceed
        100. max_simultaneous_positions is one exception: it's a
        whole-number position count, not a percentage, and 0 is a valid,
        meaningful value there (unlimited -- see RiskConfig's docstring),
        not an error like it would be for every other field.
        allow_extended_hours_trading is the other: a plain bool, no
        numeric validation applies at all. Turning it on only widens
        WHEN a signal is allowed to enter (see RiskConfig's docstring) --
        it does not by itself make the broker accept an extended-hours
        order; see WebullBrokerClient._order_payload's support_trading_
        session note for that still-separate, still-unverified half."""
        config = _resolve_loop(request).risk_engine.config
        updates = update.model_dump(exclude_none=True)
        errors = []
        for field, value in updates.items():
            if field == "allow_extended_hours_trading":
                continue
            if field == "max_simultaneous_positions":
                if value < 0:
                    errors.append(f"{field} must be 0 (unlimited) or a positive whole number.")
                continue
            if value <= 0:
                errors.append(f"{field} must be greater than 0.")
            elif field != "min_risk_reward_ratio" and value > 100:
                errors.append(f"{field} must not exceed 100.")
        if errors:
            raise HTTPException(status_code=422, detail=" ".join(errors))
        for field, value in updates.items():
            setattr(config, field, value)
        return {field: getattr(config, field) for field in _ADJUSTABLE_RISK_FIELDS}

    @app.get("/api/position-settings")
    def get_position_settings(request: Request):
        """Live values of the two PositionManagementConfig fields the
        Settings modal exposes -- read straight off the running
        PositionManager.config, distinct from RiskConfig above."""
        config = _resolve_loop(request).position_manager.config
        return {field: getattr(config, field) for field in _ADJUSTABLE_POSITION_FIELDS}

    @app.post("/api/position-settings")
    def update_position_settings(update: PositionSettingsUpdate, request: Request):
        """Mutates the live PositionManager.config in place -- takes effect
        on the very next check_exit() call for every open position, no
        restart needed. Only fields present (non-None) in the request body
        are changed. Both are percentages that must be positive and not
        exceed 100 (a trailing stop or breakeven trigger beyond 100% of
        price is meaningless)."""
        config = _resolve_loop(request).position_manager.config
        updates = update.model_dump(exclude_none=True)
        errors = [f"{field} must be greater than 0." for field, value in updates.items() if value <= 0]
        errors += [f"{field} must not exceed 100." for field, value in updates.items() if value > 100]
        if errors:
            raise HTTPException(status_code=422, detail=" ".join(errors))
        for field, value in updates.items():
            setattr(config, field, value)
        return {field: getattr(config, field) for field in _ADJUSTABLE_POSITION_FIELDS}

    @app.post("/api/kill-switch")
    def update_kill_switch(update: KillSwitchUpdate, request: Request):
        """Toggles the kill switch from the dashboard's header button.

        Engaging (`active=true`) calls
        TradingLoop.engage_kill_switch_and_flatten, which blocks all new
        entries immediately (RiskEngine checks this on every signal) and
        starts force-closing every open position at market -- the actual
        closing happens on the trading loop's own processing thread, not
        synchronously in this request, and keeps retrying every poll
        cycle until every position is actually closed or the switch is
        disengaged (fixed 2026-08-11 -- see that method's docstring for
        why a single failed close attempt used to permanently abandon the
        flatten for that position). Disengaging (`active=false`) releases
        the switch, which also stops that retry -- any position still
        open at that point is left exactly as it is."""
        loop = _resolve_loop(request)
        if update.active:
            loop.engage_kill_switch_and_flatten("Kill switch engaged from dashboard")
        else:
            loop.risk_engine.release_kill_switch()
        return {"kill_switch_active": loop.risk_engine.kill_switch_active}

    @app.get("/api/positions")
    def get_positions(request: Request):
        loop = _resolve_loop(request)
        rows = []
        for symbol, position in loop.get_open_positions().items():
            # Reads TradingLoop's own per-tick price cache
            # (get_last_known_price) instead of calling broker.get_snapshot()
            # live, once per position, on every request -- see that
            # method's docstring for the 504 incident (2026-08-12) this
            # fixes.
            current_price = loop.get_last_known_price(symbol)
            unrealized_pnl = (
                (current_price - position.avg_entry_price) * position.quantity
                if current_price is not None else None
            )
            rows.append({
                "symbol": symbol,
                "side": position.side.value,
                "quantity": position.quantity,
                "avg_entry_price": position.avg_entry_price,
                "current_price": current_price,
                "unrealized_pnl": unrealized_pnl,
                "stop_price": position.stop_price,
                "target_price": position.target_price,
                # True once a resting broker-side stop (and, before target
                # is hit, target) order is actually protecting this
                # position -- see TradingLoop._attach_broker_bracket. False
                # means this position is riding on this loop's own
                # software-side PositionManager checks alone (broker
                # doesn't support resting orders, or attaching/syncing one
                # failed and this position fell back).
                "broker_managed": position.broker_stop_order_id is not None,
                "max_favorable_excursion": position.max_favorable_excursion,
                "max_adverse_excursion": position.max_adverse_excursion,
                "opened_at": position.opened_at.isoformat(),
                "strategy_name": position.strategy_name,
            })
        return rows

    @app.post("/api/positions/{symbol}/close")
    def close_position(symbol: str, request: Request):
        """Per-position "Close" button in the Open Positions table --
        force-closes exactly this one symbol, unlike the kill switch above
        (which closes everything and requires a manual disengage
        afterward). See TradingLoop.request_manual_close's docstring: this
        also briefly pauses new entries so the close isn't left competing
        for rate-limiter slots against a flood of simultaneous entry
        attempts, self-expiring on its own with no dashboard action
        needed. The actual close happens on the trading loop's own
        processing thread and keeps retrying every poll cycle until it
        succeeds, same as the kill switch's flatten -- this endpoint only
        confirms the request was recorded, not that the position is
        already closed by the time it returns."""
        symbol = symbol.strip().upper()
        accepted = _resolve_loop(request).request_manual_close(symbol)
        if not accepted:
            raise HTTPException(status_code=404, detail=f"{symbol} is not a currently open position.")
        return {"symbol": symbol, "close_requested": True}

    @app.get("/api/risk-events")
    def get_risk_events(request: Request, limit: int = 50):
        events = _resolve_loop(request).risk_engine.events[-limit:]
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
    def get_trades(request: Request, limit: int = 100):
        user_id = _current_user_id(request)
        bot_id = _current_bot_id(request)
        with session_factory() as session:
            rows = get_recent_trades(session, user_id=user_id, bot_id=bot_id, limit=limit)
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
    def get_performance(request: Request):
        user_id = _current_user_id(request)
        bot_id = _current_bot_id(request)
        with session_factory() as session:
            return get_performance_summary(session, user_id=user_id, bot_id=bot_id)

    @app.get("/api/score-breakdown")
    def get_score_breakdown(request: Request):
        """Sanity-check aid for the MIS weighting: which components are
        actually driving scores up in practice, averaged over recent
        history for the current weights_version -- see
        db/repository.py's get_momentum_score_component_summary."""
        user_id = _current_user_id(request)
        bot_id = _current_bot_id(request)
        with session_factory() as session:
            return get_momentum_score_component_summary(session, user_id=user_id, bot_id=bot_id)

    @app.get("/api/score-history")
    def get_score_history(symbol: str, request: Request, limit: int = 50):
        user_id = _current_user_id(request)
        bot_id = _current_bot_id(request)
        with session_factory() as session:
            rows = get_momentum_scores(session, symbol=symbol.upper(), limit=limit, user_id=user_id, bot_id=bot_id)
            return [
                {
                    "timestamp": r.timestamp.isoformat(),
                    "score": r.score,
                    "weights_version": r.weights_version,
                    "components": r.components,
                }
                for r in rows
            ]

    @app.post("/api/scan-symbol")
    def scan_symbol(symbol: str, request: Request):
        """Manually runs one ticker through BroadScanner's structural
        gates right now and adds it to the live candidate list if it
        passes -- the on-demand, single-symbol equivalent of waiting for
        the next full universe rescan (which can take many minutes). See
        TradingLoop.scan_and_add_candidate. `state` is the resulting
        candidate's real CandidateState value when it exists (whether
        newly added or already tracked), or the literal string "rejected"
        when BroadScanner's structural gates turned it away outright (no
        Candidate object -- and therefore no CandidateState -- ever gets
        created in that case), or the literal string "pending" if the
        underlying broker call hasn't finished within
        _SCAN_SYMBOL_TIMEOUT_SECONDS -- see this module's
        _scan_symbol_executor comment for why the scan itself keeps
        running in the background rather than being abandoned; the
        dashboard should just let the user retry the request shortly."""
        symbol = symbol.strip().upper()
        loop = _resolve_loop(request)
        future = _scan_symbol_executor.submit(loop.scan_and_add_candidate, symbol)
        try:
            candidate, reason, was_newly_added = future.result(timeout=_SCAN_SYMBOL_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError:
            return {
                "symbol": symbol,
                "added": False,
                "already_tracked": False,
                "state": "pending",
                "reason": (
                    "Still checking against a rate-limited broker connection -- "
                    "the scan is continuing in the background, try again in a "
                    "few seconds."
                ),
                "score": None,
            }
        if candidate is None:
            return {
                "symbol": symbol,
                "added": False,
                "already_tracked": False,
                "state": "rejected",
                "reason": reason,
                "score": None,
            }
        return {
            "symbol": candidate.symbol,
            "added": was_newly_added,
            "already_tracked": not was_newly_added,
            "state": candidate.state.value,
            "reason": _last_transition_reason(candidate.notes),
            "score": candidate.latest_score.score if candidate.latest_score else None,
        }

    @app.get("/api/bots")
    def get_bots(request: Request):
        """The current user's bots for the header's hamburger menu
        (2026-08-15 multi-bot framework) -- today, always exactly one
        ("Day Trading Quant"), created at signup (auth/routes.py) or by
        the 0003 migration's backfill for any pre-existing user. Empty
        list in single-tenant/no-auth mode (no user to look bots up
        for), matching every other endpoint's behavior there."""
        user_id = _current_user_id(request)
        if user_id is None:
            return []
        with session_factory() as session:
            rows = session.query(Bot).filter(Bot.user_id == user_id, Bot.is_active.is_(True)).all()
            return [{"id": b.id, "slug": b.slug, "name": b.name} for b in rows]

    # Landing/signup/login/app pages (2026-08-15 multi-tenant conversion).
    # Registered as explicit routes -- Starlette matches routes in
    # registration order, so these take priority over the "/" StaticFiles
    # mount below (which still serves style.css/app.js/auth.js/logo.png
    # etc.) rather than that mount's html=True auto-serving app.html for
    # every unmatched path the way it used to serve index.html.

    @app.get("/", response_class=FileResponse)
    def landing_page():
        return FileResponse(_STATIC_DIR / "landing.html")

    @app.get("/signup", response_class=FileResponse)
    def signup_page():
        return FileResponse(_STATIC_DIR / "signup.html")

    @app.get("/login", response_class=FileResponse)
    def login_page():
        return FileResponse(_STATIC_DIR / "login.html")

    @app.get("/pricing", response_class=FileResponse)
    def pricing_page():
        return FileResponse(_STATIC_DIR / "pricing.html")

    @app.get("/features", response_class=FileResponse)
    def features_page():
        return FileResponse(_STATIC_DIR / "features.html")

    @app.get("/strategies", response_class=FileResponse)
    def strategies_page():
        return FileResponse(_STATIC_DIR / "strategies.html")

    def _require_login_page(request: Request, page_filename: str):
        """Shared guard for every page under /app: redirects to /login when
        auth is configured and the request has no valid session, otherwise
        serves the page unchanged -- including when auth isn't configured
        at all, so the existing single-tenant deployment keeps working
        with no login step until an operator opts in (see create_app's
        session_secret_key guard above). Reuses _current_user (the same
        auth check every /api/* endpoint uses above) rather than a
        second, separate get_current_user instance."""
        try:
            _current_user(request)
        except HTTPException:
            return RedirectResponse(url="/login")
        return FileResponse(_STATIC_DIR / page_filename)

    @app.get("/app")
    def app_page(request: Request):
        return _require_login_page(request, "app.html")

    @app.get("/app/settings")
    def app_settings_page(request: Request):
        return _require_login_page(request, "app_settings.html")

    if _STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")

    return app
