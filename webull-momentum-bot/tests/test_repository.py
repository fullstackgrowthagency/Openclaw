from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from webull_bot.db.models import Base, OrderRecord, TradeRecord
from webull_bot.db.repository import get_performance_summary, get_recent_trades, record_order, record_trade
from webull_bot.enums import ExitReason, OrderSide, OrderStatus, OrderType
from webull_bot.models import Order, Trade


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _trade(**overrides) -> Trade:
    base = dict(
        symbol="AAPL", strategy_name="momentum_breakout", side=OrderSide.BUY,
        entry_price=10.0, exit_price=11.0, quantity=100,
        opened_at=datetime(2026, 1, 1, 9, 31), closed_at=datetime(2026, 1, 1, 9, 45),
        exit_reason=ExitReason.PROFIT_TARGET, pnl=100.0, pnl_pct=10.0,
        max_favorable_excursion=12.0, max_adverse_excursion=1.0,
    )
    base.update(overrides)
    return Trade(**base)


def _order(**overrides) -> Order:
    base = dict(
        symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=100,
        status=OrderStatus.SUBMITTED, client_order_id="abc-123", broker_order_id="abc-123",
        strategy_name="momentum_breakout",
    )
    base.update(overrides)
    return Order(**base)


def test_record_trade_persists(session):
    record_trade(session, _trade(), trading_mode="paper")
    session.commit()
    rows = session.query(TradeRecord).all()
    assert len(rows) == 1
    assert rows[0].symbol == "AAPL"
    assert rows[0].pnl == 100.0
    assert rows[0].exit_reason == "profit_target"


def test_record_order_inserts_then_updates_same_row(session):
    record_order(session, _order(status=OrderStatus.SUBMITTED), trading_mode="paper")
    session.commit()
    assert session.query(OrderRecord).count() == 1

    record_order(session, _order(status=OrderStatus.FILLED), trading_mode="paper")
    session.commit()

    rows = session.query(OrderRecord).all()
    assert len(rows) == 1  # updated in place, not a second row
    assert rows[0].status == "filled"


def test_record_order_requires_client_order_id(session):
    with pytest.raises(ValueError):
        record_order(session, _order(client_order_id=None), trading_mode="paper")


def test_get_recent_trades_orders_by_closed_at_desc(session):
    record_trade(session, _trade(closed_at=datetime(2026, 1, 1, 9, 45)), trading_mode="paper")
    record_trade(session, _trade(closed_at=datetime(2026, 1, 2, 9, 45)), trading_mode="paper")
    session.commit()
    rows = get_recent_trades(session, limit=10)
    assert rows[0].closed_at > rows[1].closed_at


def test_get_performance_summary_empty(session):
    summary = get_performance_summary(session)
    assert summary["total_trades"] == 0
    assert summary["win_rate"] == 0.0


def test_get_performance_summary_computes_win_rate_and_pnl(session):
    record_trade(session, _trade(pnl=100.0, pnl_pct=10.0), trading_mode="paper")
    record_trade(session, _trade(pnl=-50.0, pnl_pct=-5.0, exit_reason=ExitReason.STOP_LOSS), trading_mode="paper")
    session.commit()
    summary = get_performance_summary(session)
    assert summary["total_trades"] == 2
    assert summary["win_rate"] == 0.5
    assert summary["total_pnl"] == 50.0
