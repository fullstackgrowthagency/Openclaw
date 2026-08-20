"""
Thin persistence layer between in-memory domain objects (models.py) and the
SQLAlchemy schema (db/models.py). Kept separate from TradingLoop so the
orchestrator never imports the DB layer directly -- it only calls the
on_trade_closed/on_order_update/on_state_transition/on_score_computed hooks
it already exposes (plus an optional MomentumEventTracker collaborator for
momentum events), and callers (main.py, scripts/run_dashboard.py) decide
whether/how to persist.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Callable, Optional

from sqlalchemy.orm import Session

from ..collection.event_recorder import EventRecorder
from ..enums import CandidateState, OrderSide
from ..models import MomentumEvent, MomentumScore, Order, Position, Trade
from ..scoring.momentum_ignition_score import MISConfig
from .models import Bot, MomentumEventRecord, MomentumScoreRecord, OrderRecord, PositionRecord, ScannerEvent, TradeRecord

_DEFAULT_BOT_SLUG = "day-trading-quant"
_DEFAULT_BOT_NAME = "Day Trading Quant"
_DEFAULT_BOT_KIND = "momentum_quant"


def get_or_create_default_bot(session: Session, user_id: int) -> Bot:
    """Every user's first (today, only) bot -- the existing Day Trading
    Quant momentum system, registered into the multi-bot framework
    (2026-08-15). Idempotent (keyed on the (user_id, slug) unique
    constraint -- see db/models.py's Bot) so signup-time creation
    (auth/routes.py) and the run_dashboard.py startup sweep's lazy
    fallback always land on the same row rather than racing to create
    duplicates."""
    bot = (
        session.query(Bot)
        .filter(Bot.user_id == user_id, Bot.slug == _DEFAULT_BOT_SLUG)
        .one_or_none()
    )
    if bot is None:
        bot = Bot(user_id=user_id, slug=_DEFAULT_BOT_SLUG, name=_DEFAULT_BOT_NAME, kind=_DEFAULT_BOT_KIND)
        session.add(bot)
        session.flush()
    return bot


def record_trade(
    session: Session, trade: Trade, *, trading_mode: str, user_id: Optional[int] = None, bot_id: Optional[int] = None,
) -> TradeRecord:
    record = TradeRecord(
        user_id=user_id,
        bot_id=bot_id,
        symbol=trade.symbol,
        strategy_name=trade.strategy_name,
        side=trade.side.value,
        entry_price=trade.entry_price,
        exit_price=trade.exit_price,
        quantity=trade.quantity,
        opened_at=trade.opened_at,
        closed_at=trade.closed_at,
        exit_reason=trade.exit_reason.value,
        pnl=trade.pnl,
        pnl_pct=trade.pnl_pct,
        max_favorable_excursion=trade.max_favorable_excursion,
        max_adverse_excursion=trade.max_adverse_excursion,
        trading_mode=trading_mode,
    )
    session.add(record)
    session.flush()
    return record


def record_order(
    session: Session, order: Order, *, trading_mode: str, user_id: Optional[int] = None, bot_id: Optional[int] = None,
) -> OrderRecord:
    """Upsert by client_order_id: an order is written once on submission and
    again every time its status changes (SUBMITTED -> FILLED, etc.), so this
    must update the existing row rather than violate the unique constraint
    on client_order_id with a second insert. Also filtered by user_id
    (2026-08-15) -- client_order_id is a UUID (no realistic cross-user
    collision), but scoping the lookup is free insurance and keeps the
    query's intent explicit: "this user's order with this id," not just
    "any order anywhere with this id." bot_id (2026-08-15 multi-bot
    framework) is the same free-insurance scoping one level deeper."""
    if not order.client_order_id:
        raise ValueError("Order.client_order_id is required to persist an order")

    existing = (
        session.query(OrderRecord)
        .filter(
            OrderRecord.client_order_id == order.client_order_id,
            OrderRecord.user_id == user_id,
            OrderRecord.bot_id == bot_id,
        )
        .one_or_none()
    )
    if existing is None:
        existing = OrderRecord(
            client_order_id=order.client_order_id, trading_mode=trading_mode, user_id=user_id, bot_id=bot_id,
        )
        session.add(existing)

    existing.broker_order_id = order.broker_order_id
    existing.symbol = order.symbol
    existing.side = order.side.value
    existing.order_type = order.order_type.value
    existing.quantity = order.quantity
    existing.limit_price = order.limit_price
    existing.stop_price = order.stop_price
    existing.status = order.status.value
    existing.strategy_name = order.strategy_name
    existing.updated_at = order.updated_at
    session.flush()
    return existing


def upsert_open_position(
    session: Session, position: Position, *, user_id: Optional[int] = None, bot_id: Optional[int] = None,
) -> PositionRecord:
    """Write-through counterpart to TradingLoop's self._positions[symbol] =
    position -- called at every point that dict gains a symbol or has that
    symbol's quantity change (entry fill, reconcile adoption, partial
    exit), NEVER on a plain per-tick price/MFE update (see
    PositionRecord's docstring) -- keeps this off the hot tick path.
    Upserts by (user_id, bot_id, symbol) the same way record_order upserts
    by client_order_id, since sync_schema() won't retroactively add
    PositionRecord's UniqueConstraint to an already-existing table."""
    existing = (
        session.query(PositionRecord)
        .filter(
            PositionRecord.user_id == user_id,
            PositionRecord.bot_id == bot_id,
            PositionRecord.symbol == position.symbol,
        )
        .one_or_none()
    )
    if existing is None:
        existing = PositionRecord(user_id=user_id, bot_id=bot_id, symbol=position.symbol)
        session.add(existing)

    existing.side = position.side.value
    existing.quantity = position.quantity
    existing.avg_entry_price = position.avg_entry_price
    existing.stop_price = position.stop_price
    existing.target_price = position.target_price
    existing.strategy_name = position.strategy_name
    existing.opened_at = position.opened_at
    existing.broker_stop_order_id = position.broker_stop_order_id
    existing.broker_target_order_id = position.broker_target_order_id
    session.flush()
    return existing


def delete_open_position(
    session: Session, symbol: str, *, user_id: Optional[int] = None, bot_id: Optional[int] = None,
) -> None:
    """Write-through counterpart to TradingLoop's self._positions.pop(symbol)
    -- called from every path that closes a position (full exit, confirmed
    external close). A no-op if no row exists for this scope/symbol (e.g.
    this position was never open long enough to have been upserted, or
    it's already been deleted)."""
    session.query(PositionRecord).filter(
        PositionRecord.user_id == user_id,
        PositionRecord.bot_id == bot_id,
        PositionRecord.symbol == symbol,
    ).delete()
    session.flush()


def get_open_positions_snapshot(
    session: Session, *, user_id: Optional[int] = None, bot_id: Optional[int] = None,
) -> list[Position]:
    """Reconstructs Position objects (models.Position, not the ORM row)
    from this scope's persisted snapshot -- used exactly once, at process
    startup, by TradingLoop's very first reconcile_positions_from_broker
    call (see that method and TradingLoop._load_position_snapshot).
    max_favorable_excursion/max_adverse_excursion are not persisted (see
    PositionRecord's docstring) so every reconstructed Position starts
    those at 0.0 -- a Trade built from one of these via
    _build_trade_for_external_close will carry mfe/mae=0.0, same
    best-effort tradeoff as its exit_price fallback chain."""
    rows = (
        session.query(PositionRecord)
        .filter(PositionRecord.user_id == user_id, PositionRecord.bot_id == bot_id)
        .all()
    )
    return [
        Position(
            symbol=row.symbol,
            side=OrderSide(row.side),
            quantity=row.quantity,
            avg_entry_price=row.avg_entry_price,
            stop_price=row.stop_price,
            target_price=row.target_price,
            trailing_stop_pct=None,
            opened_at=row.opened_at,
            strategy_name=row.strategy_name,
            broker_stop_order_id=row.broker_stop_order_id,
            broker_target_order_id=row.broker_target_order_id,
        )
        for row in rows
    ]


def get_recent_trades(
    session: Session, *, user_id: Optional[int] = None, bot_id: Optional[int] = None, limit: int = 100,
) -> list[TradeRecord]:
    return (
        session.query(TradeRecord)
        .filter(TradeRecord.user_id == user_id, TradeRecord.bot_id == bot_id)
        .order_by(TradeRecord.closed_at.desc())
        .limit(limit)
        .all()
    )


def get_performance_summary(session: Session, *, user_id: Optional[int] = None, bot_id: Optional[int] = None) -> dict:
    trades = (
        session.query(TradeRecord)
        .filter(TradeRecord.user_id == user_id, TradeRecord.bot_id == bot_id)
        .all()
    )
    total_trades = len(trades)
    if total_trades == 0:
        return {"total_trades": 0, "win_rate": 0.0, "total_pnl": 0.0, "total_pnl_pct": 0.0, "avg_pnl_pct": 0.0}

    wins = sum(1 for t in trades if t.pnl > 0)
    total_pnl = sum(t.pnl for t in trades)
    # Total dollar P&L as a % of total capital actually deployed (Σ
    # entry_price * quantity across every trade) -- a genuine "return on
    # capital put to work" figure, distinct from avg_pnl_pct below (an
    # equal-weighted average of each trade's OWN % return, which treats a
    # $100 trade and a $100,000 trade as equally significant). Guards
    # against a zero-cost-basis divide, though that shouldn't happen for
    # any real trade (entry_price/quantity are always positive).
    total_cost_basis = sum(t.entry_price * t.quantity for t in trades)
    return {
        "total_trades": total_trades,
        "win_rate": wins / total_trades,
        "total_pnl": total_pnl,
        "total_pnl_pct": (total_pnl / total_cost_basis * 100.0) if total_cost_basis else 0.0,
        "avg_pnl_pct": sum(t.pnl_pct for t in trades) / total_trades,
    }


def record_scanner_event(
    session: Session,
    *,
    symbol: str,
    from_state: CandidateState,
    to_state: CandidateState,
    timestamp: datetime,
    reason: str,
    user_id: Optional[int] = None,
    bot_id: Optional[int] = None,
) -> ScannerEvent:
    record = ScannerEvent(
        user_id=user_id,
        bot_id=bot_id,
        symbol=symbol,
        timestamp=timestamp,
        event_type="state_transition",
        from_state=from_state.value,
        to_state=to_state.value,
        reason=reason,
    )
    session.add(record)
    session.flush()
    return record


def record_momentum_score(
    session: Session, score: MomentumScore, *, user_id: Optional[int] = None, bot_id: Optional[int] = None,
) -> MomentumScoreRecord:
    record = MomentumScoreRecord(
        user_id=user_id,
        bot_id=bot_id,
        symbol=score.symbol,
        timestamp=score.timestamp,
        score=score.score,
        weights_version=score.weights_version,
        components=asdict(score.components),
    )
    session.add(record)
    session.flush()
    return record


def get_momentum_scores(
    session: Session, *, symbol: Optional[str] = None, limit: int = 200,
    user_id: Optional[int] = None, bot_id: Optional[int] = None,
) -> list[MomentumScoreRecord]:
    """Most-recent-first raw history, optionally filtered to one symbol --
    for inspecting a specific candidate's score/component trajectory over
    time (e.g. dashboard/app.py's /api/score-history)."""
    query = session.query(MomentumScoreRecord).filter(
        MomentumScoreRecord.user_id == user_id, MomentumScoreRecord.bot_id == bot_id,
    )
    if symbol is not None:
        query = query.filter(MomentumScoreRecord.symbol == symbol)
    return query.order_by(MomentumScoreRecord.timestamp.desc()).limit(limit).all()


def get_momentum_score_component_summary(
    session: Session, *, weights_version: Optional[str] = None, limit: int = 500,
    mis_config: Optional[MISConfig] = None, user_id: Optional[int] = None, bot_id: Optional[int] = None,
) -> dict:
    """Sanity-check aid for tuning weights.yaml: averages each component's
    raw (0-100) sub-score across recent MomentumScoreRecord rows and
    multiplies by that component's current normalized weight, so you can
    see which components are actually driving scores up in practice rather
    than just which ones you *intended* to emphasize -- e.g. after
    reweighting towards "popularity" components (see weights.yaml's v2
    comment), this is how you'd confirm relative_volume_score/
    float_turnover_score etc. are actually the top contributors, not just
    the ones with the biggest configured weight.

    Deliberately scoped to a single weights_version (the most recent one
    seen, unless one is passed explicitly): components differ between
    versions (v1 had 8, v2 added 3 more -- see MomentumScoreComponents),
    so averaging across versions would silently mix incomparable formulas.
    Rows from any other version are excluded from the average, not zero-filled.
    """
    config = mis_config or MISConfig.load()

    if weights_version is None:
        latest = (
            session.query(MomentumScoreRecord.weights_version)
            .filter(MomentumScoreRecord.user_id == user_id, MomentumScoreRecord.bot_id == bot_id)
            .order_by(MomentumScoreRecord.timestamp.desc())
            .first()
        )
        weights_version = latest[0] if latest else None

    rows = (
        session.query(MomentumScoreRecord)
        .filter(
            MomentumScoreRecord.weights_version == weights_version,
            MomentumScoreRecord.user_id == user_id,
            MomentumScoreRecord.bot_id == bot_id,
        )
        .order_by(MomentumScoreRecord.timestamp.desc())
        .limit(limit)
        .all()
        if weights_version is not None
        else []
    )

    components = []
    for name, weight in config.weights.items():
        # room_to_target_score (v2.2, 2026-08-13) can be persisted as None --
        # it's the one component allowed to be unavailable rather than a
        # real 0-100 value (see MomentumScoreComponents.room_to_target_score's
        # docstring) -- so a row missing it entirely is excluded from this
        # average the same way a row from a different weights_version is,
        # not treated as a 0 that would silently drag the average down.
        values = [r.components[name] for r in rows if name in r.components and r.components[name] is not None]
        if not values:
            continue
        avg_raw_score = sum(values) / len(values)
        components.append({
            "name": name,
            "weight": weight,
            "sample_size": len(values),
            "avg_raw_score": avg_raw_score,
            "avg_weighted_contribution": avg_raw_score * weight,
        })

    total_contribution = sum(c["avg_weighted_contribution"] for c in components) or 1.0
    for c in components:
        c["pct_of_avg_score"] = c["avg_weighted_contribution"] / total_contribution * 100.0
    components.sort(key=lambda c: c["avg_weighted_contribution"], reverse=True)

    return {
        "weights_version": weights_version,
        "sample_size": len(rows),
        "components": components,
    }


def _metrics_to_json(metrics) -> Optional[dict]:
    if metrics is None:
        return None
    data = asdict(metrics)
    data["timestamp"] = metrics.timestamp.isoformat()  # datetime isn't JSON-serializable as-is
    return data


def record_momentum_event(
    session: Session, event: MomentumEvent, *, existing_id: Optional[int] = None,
    user_id: Optional[int] = None, bot_id: Optional[int] = None,
) -> MomentumEventRecord:
    """Upsert by `existing_id` (the DB row id) rather than a natural key --
    unlike orders (client_order_id) or trades, a momentum event has no
    natural unique identifier of its own. Callers that need to update the
    same row repeatedly (see DBBackedEventRecorder below) must remember the
    id returned by the first call and pass it back in on subsequent calls.
    `user_id`/`bot_id` are only applied on first insert -- existing_id
    already uniquely (and privately, per-DBBackedEventRecorder-instance)
    identifies the row, so there's nothing to re-scope on update."""
    record = session.get(MomentumEventRecord, existing_id) if existing_id is not None else None
    if record is None:
        record = MomentumEventRecord(
            user_id=user_id,
            bot_id=bot_id,
            symbol=event.symbol,
            detected_at=event.detected_at,
            trigger_reason=event.trigger_reason,
            price_at_event=event.price_at_event,
        )
        session.add(record)

    record.was_traded = event.was_traded
    record.score_at_event = event.score_at_event
    record.metrics_at_event = _metrics_to_json(event.metrics_at_event)
    record.outcome_30s = event.outcome_30s
    record.outcome_1m = event.outcome_1m
    record.outcome_3m = event.outcome_3m
    record.outcome_5m = event.outcome_5m
    record.outcome_10m = event.outcome_10m
    record.outcome_15m = event.outcome_15m
    record.max_favorable_excursion_pct = event.max_favorable_excursion_pct
    record.max_adverse_excursion_pct = event.max_adverse_excursion_pct
    record.hod_broken = event.hod_broken
    record.vwap_failed = event.vwap_failed
    record.outcome_label = event.outcome_label.value
    # -- Real-Time Momentum Qualification Layer (2026-08-17) --------------
    record.outcome_5s = event.outcome_5s
    record.outcome_10s = event.outcome_10s
    record.outcome_15s = event.outcome_15s
    record.momentum_qualification_at_event = event.momentum_qualification_at_event
    session.flush()
    return record


class DBBackedEventRecorder(EventRecorder):
    """Write-through EventRecorder: keeps the in-memory dict the base class
    already provides (MomentumEventTracker's polling logic reads events back
    via `get()`, so that must keep working identically) while also
    persisting every save()/update() to the database.

    Uses a fresh, short-lived Session per call rather than one long-lived
    session, since MomentumEventTracker.on_snapshot() (which drives update())
    is typically called from TradingLoop's poll loop over a span of up to 15
    minutes per event -- holding one session open that whole time would be
    both wasteful and fragile across reconnects.
    """

    def __init__(
        self, session_factory: Callable[[], Session], user_id: Optional[int] = None, bot_id: Optional[int] = None,
    ):
        super().__init__()
        self.session_factory = session_factory
        self.user_id = user_id
        self.bot_id = bot_id
        self._db_ids: dict[int, int] = {}  # in-memory event_id -> DB row id

    def save(self, event: MomentumEvent) -> int:
        event_id = super().save(event)
        with self.session_factory() as session:
            try:
                record = record_momentum_event(session, event, user_id=self.user_id, bot_id=self.bot_id)
                session.commit()
                self._db_ids[event_id] = record.id
            except Exception:
                session.rollback()
                raise
        return event_id

    def update(self, event_id: int) -> None:
        super().update(event_id)
        event = self.get(event_id)
        with self.session_factory() as session:
            try:
                record = record_momentum_event(session, event, existing_id=self._db_ids.get(event_id))
                session.commit()
                self._db_ids[event_id] = record.id
            except Exception:
                session.rollback()
                raise
