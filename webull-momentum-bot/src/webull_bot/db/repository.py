"""
Thin persistence layer between in-memory domain objects (models.py) and the
SQLAlchemy schema (db/models.py). Kept separate from TradingLoop so the
orchestrator never imports the DB layer directly -- it only calls the
on_trade_closed/on_order_update callbacks it already exposes, and callers
(main.py, scripts/run_dashboard.py) decide whether/how to persist.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import Order, Trade
from .models import OrderRecord, TradeRecord


def record_trade(session: Session, trade: Trade, *, trading_mode: str) -> TradeRecord:
    record = TradeRecord(
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


def record_order(session: Session, order: Order, *, trading_mode: str) -> OrderRecord:
    """Upsert by client_order_id: an order is written once on submission and
    again every time its status changes (SUBMITTED -> FILLED, etc.), so this
    must update the existing row rather than violate the unique constraint
    on client_order_id with a second insert."""
    if not order.client_order_id:
        raise ValueError("Order.client_order_id is required to persist an order")

    existing = (
        session.query(OrderRecord)
        .filter(OrderRecord.client_order_id == order.client_order_id)
        .one_or_none()
    )
    if existing is None:
        existing = OrderRecord(client_order_id=order.client_order_id, trading_mode=trading_mode)
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


def get_recent_trades(session: Session, limit: int = 100) -> list[TradeRecord]:
    return (
        session.query(TradeRecord)
        .order_by(TradeRecord.closed_at.desc())
        .limit(limit)
        .all()
    )


def get_performance_summary(session: Session) -> dict:
    trades = session.query(TradeRecord).all()
    total_trades = len(trades)
    if total_trades == 0:
        return {"total_trades": 0, "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl_pct": 0.0}

    wins = sum(1 for t in trades if t.pnl > 0)
    return {
        "total_trades": total_trades,
        "win_rate": wins / total_trades,
        "total_pnl": sum(t.pnl for t in trades),
        "avg_pnl_pct": sum(t.pnl_pct for t in trades) / total_trades,
    }
