from datetime import datetime

import pytest

from fx_bot.enums import ExitReason, OrderSide
from fx_bot.models import MarketSnapshot, Position
from fx_bot.position.position_manager import PositionManager, PositionManagementConfig


def _long_position(stop_price=1.0980, target_price=1.1040, avg_entry_price=1.1000) -> Position:
    return Position(
        symbol="EUR/USD", side=OrderSide.BUY, quantity=10_000, avg_entry_price=avg_entry_price,
        stop_price=stop_price, target_price=target_price, trailing_stop_pips=None,
        opened_at=datetime.utcnow(), strategy_name="test",
    )


def _short_position(stop_price=1.1020, target_price=1.0960, avg_entry_price=1.1000) -> Position:
    return Position(
        symbol="EUR/USD", side=OrderSide.SELL, quantity=10_000, avg_entry_price=avg_entry_price,
        stop_price=stop_price, target_price=target_price, trailing_stop_pips=None,
        opened_at=datetime.utcnow(), strategy_name="test",
    )


def _snapshot(bid, ask) -> MarketSnapshot:
    return MarketSnapshot(symbol="EUR/USD", timestamp=datetime.utcnow(), bid=bid, ask=ask)


# -- plain stop/target, no trailing/breakeven configured ---------------------

def test_long_position_holds_when_price_is_between_stop_and_target():
    manager = PositionManager()
    position = _long_position()
    assert manager.manage(position, _snapshot(bid=1.1010, ask=1.1012)) is None


def test_long_position_stop_loss_triggers_on_the_bid():
    manager = PositionManager()
    position = _long_position(stop_price=1.0980)
    assert manager.manage(position, _snapshot(bid=1.0979, ask=1.0981)) == ExitReason.STOP_LOSS


def test_long_position_profit_target_triggers_on_the_bid():
    manager = PositionManager()
    position = _long_position(target_price=1.1040)
    assert manager.manage(position, _snapshot(bid=1.1041, ask=1.1043)) == ExitReason.PROFIT_TARGET


def test_short_position_stop_loss_triggers_on_the_ask():
    manager = PositionManager()
    position = _short_position(stop_price=1.1020)
    assert manager.manage(position, _snapshot(bid=1.1019, ask=1.1021)) == ExitReason.STOP_LOSS


def test_short_position_profit_target_triggers_on_the_ask():
    manager = PositionManager()
    position = _short_position(target_price=1.0960)
    assert manager.manage(position, _snapshot(bid=1.0958, ask=1.0959)) == ExitReason.PROFIT_TARGET


def test_stop_takes_priority_over_target_if_somehow_both_are_hit_the_same_tick():
    manager = PositionManager()
    # Deliberately inverted/degenerate config (target below stop for a
    # long) so a single price can satisfy both checks at once: 1.0990 is
    # both <= stop_price (1.1000) and >= target_price (1.0980).
    position = _long_position(stop_price=1.1000, target_price=1.0980)
    assert manager.manage(position, _snapshot(bid=1.0990, ask=1.0992)) == ExitReason.STOP_LOSS


def test_none_stop_or_target_never_triggers():
    manager = PositionManager()
    position = _long_position(stop_price=None, target_price=None)
    assert manager.manage(position, _snapshot(bid=0.5, ask=0.5)) is None


# -- breakeven ---------------------------------------------------------------

def test_breakeven_moves_a_long_stop_to_entry_once_triggered():
    manager = PositionManager(PositionManagementConfig(breakeven_trigger_pips=15))
    position = _long_position(avg_entry_price=1.1000, stop_price=1.0980)
    manager.manage(position, _snapshot(bid=1.1016, ask=1.1018))  # +16 pips, past the 15-pip trigger
    assert position.stop_price == pytest.approx(1.1000)


def test_breakeven_does_not_move_a_long_stop_before_the_trigger():
    manager = PositionManager(PositionManagementConfig(breakeven_trigger_pips=15))
    position = _long_position(avg_entry_price=1.1000, stop_price=1.0980)
    manager.manage(position, _snapshot(bid=1.1005, ask=1.1007))  # only +5 pips
    assert position.stop_price == pytest.approx(1.0980)


def test_breakeven_never_moves_stop_backward_once_already_past_entry():
    manager = PositionManager(PositionManagementConfig(breakeven_trigger_pips=15))
    position = _long_position(avg_entry_price=1.1000, stop_price=1.1005)  # already better than breakeven
    manager.manage(position, _snapshot(bid=1.1016, ask=1.1018))
    assert position.stop_price == pytest.approx(1.1005)  # untouched, not reset back to 1.1000


def test_breakeven_moves_a_short_stop_to_entry_once_triggered():
    manager = PositionManager(PositionManagementConfig(breakeven_trigger_pips=15))
    position = _short_position(avg_entry_price=1.1000, stop_price=1.1020)
    manager.manage(position, _snapshot(bid=1.0982, ask=1.0984))  # -16 pips in price, a profit for a short
    assert position.stop_price == pytest.approx(1.1000)


# -- trailing stop -------------------------------------------------------

def test_trailing_stop_follows_a_rising_long_position():
    manager = PositionManager(PositionManagementConfig(trailing_stop_pips=10))
    position = _long_position(avg_entry_price=1.1000, stop_price=1.0980)
    manager.manage(position, _snapshot(bid=1.1030, ask=1.1032))
    assert position.stop_price == pytest.approx(1.1030 - 0.0010)


def test_trailing_stop_never_loosens_a_long_position_on_a_pullback():
    manager = PositionManager(PositionManagementConfig(trailing_stop_pips=10))
    position = _long_position(avg_entry_price=1.1000, stop_price=1.0980)
    manager.manage(position, _snapshot(bid=1.1030, ask=1.1032))
    tightened_stop = position.stop_price
    manager.manage(position, _snapshot(bid=1.1010, ask=1.1012))  # price pulls back
    assert position.stop_price == pytest.approx(tightened_stop)  # unchanged, not loosened


def test_trailing_stop_follows_a_falling_short_position():
    manager = PositionManager(PositionManagementConfig(trailing_stop_pips=10))
    position = _short_position(avg_entry_price=1.1000, stop_price=1.1020)
    manager.manage(position, _snapshot(bid=1.0968, ask=1.0970))
    assert position.stop_price == pytest.approx(1.0970 + 0.0010)


def test_trailing_stop_can_eventually_trigger_its_own_exit():
    manager = PositionManager(PositionManagementConfig(trailing_stop_pips=10))
    position = _long_position(avg_entry_price=1.1000, stop_price=1.0980)
    assert manager.manage(position, _snapshot(bid=1.1030, ask=1.1032)) is None  # trails to 1.1020
    result = manager.manage(position, _snapshot(bid=1.1019, ask=1.1021))  # pulls back through the trailed stop
    assert result == ExitReason.STOP_LOSS
