from datetime import datetime

import pytest

from fx_bot.enums import ExitReason, OrderSide, OrderStatus, OrderType, SignalAction
from fx_bot.models import MarketSnapshot, Order, Position, Signal, Trade


def test_market_snapshot_mid_and_spread_pips():
    snapshot = MarketSnapshot(symbol="EUR/USD", timestamp=datetime.utcnow(), bid=1.1000, ask=1.1002)
    assert snapshot.mid == pytest.approx(1.1001)
    assert snapshot.spread_pips == pytest.approx(2.0)


def test_market_snapshot_spread_pips_accounts_for_jpy_pip_size():
    snapshot = MarketSnapshot(symbol="USD/JPY", timestamp=datetime.utcnow(), bid=150.00, ask=150.03)
    assert snapshot.spread_pips == pytest.approx(3.0)


def test_order_defaults_have_no_attached_bracket():
    order = Order(symbol="EUR/USD", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=100_000)
    assert order.status == OrderStatus.PENDING
    assert order.stop_loss_price is None
    assert order.take_profit_price is None


def test_order_can_carry_bracket_on_open():
    order = Order(
        symbol="EUR/USD", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=100_000,
        stop_loss_price=1.0980, take_profit_price=1.1040,
    )
    assert order.stop_loss_price == 1.0980
    assert order.take_profit_price == 1.1040


def test_position_swap_defaults_to_zero():
    position = Position(
        symbol="EUR/USD", side=OrderSide.BUY, quantity=100_000, avg_entry_price=1.1000,
        stop_price=1.0980, target_price=1.1040, trailing_stop_pips=None,
        opened_at=datetime.utcnow(), strategy_name="test",
    )
    assert position.swap == 0.0


def test_signal_carries_suggested_stop_and_target():
    signal = Signal(
        symbol="EUR/USD", action=SignalAction.ENTER_LONG, generated_at=datetime.utcnow(),
        strategy_name="test", strategy_version="v1", reference_price=1.1000,
        suggested_stop=1.0980, suggested_target=1.1040,
    )
    assert signal.suggested_stop == 1.0980
    assert signal.suggested_target == 1.1040


def test_trade_records_exit_reason_and_swap():
    trade = Trade(
        symbol="EUR/USD", strategy_name="test", side=OrderSide.BUY,
        entry_price=1.1000, exit_price=1.1040, quantity=100_000,
        opened_at=datetime.utcnow(), closed_at=datetime.utcnow(),
        exit_reason=ExitReason.PROFIT_TARGET, pnl=400.0, pnl_pct=0.36,
        max_favorable_excursion=400.0, max_adverse_excursion=-50.0,
    )
    assert trade.exit_reason == ExitReason.PROFIT_TARGET
    assert trade.swap == 0.0
