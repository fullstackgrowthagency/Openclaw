from datetime import datetime, timedelta

from webull_bot.enums import ExitReason, OrderSide, SignalAction
from webull_bot.models import MarketSnapshot, Position
from webull_bot.position.position_manager import PositionManagementConfig, PositionManager


def _position(**overrides) -> Position:
    base = dict(
        symbol="ABCD", side=OrderSide.BUY, quantity=10, avg_entry_price=10.0,
        stop_price=9.7, target_price=11.0, trailing_stop_pct=None,
        opened_at=datetime.utcnow(), strategy_name="test",
    )
    base.update(overrides)
    return Position(**base)


def _snapshot(price: float, vwap: float = 10.0, t: datetime = None) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="ABCD", timestamp=t or datetime.utcnow(), last_price=price,
        bid=price - 0.01, ask=price + 0.01, bid_size=100, ask_size=100,
        cumulative_volume=200_000, vwap=vwap, high_of_day=price, low_of_day=9.0, open_price=9.5,
    )


# -- breakeven rule ----------------------------------------------------------

def test_breakeven_moves_stop_to_entry_once_trigger_pct_reached():
    manager = PositionManager(PositionManagementConfig(breakeven_trigger_pct=5.0, trailing_stop_pct=None, exit_on_vwap_failure=False, time_limit_minutes=None))
    position = _position(stop_price=9.5, target_price=None)  # no target -- isolate the breakeven mechanic
    manager.check_exit(position, _snapshot(10.5))  # exactly +5%
    assert position.stop_price == 10.0  # avg_entry_price


def test_breakeven_does_not_fire_below_trigger():
    manager = PositionManager(PositionManagementConfig(breakeven_trigger_pct=5.0, trailing_stop_pct=None, exit_on_vwap_failure=False, time_limit_minutes=None))
    position = _position(stop_price=9.5, target_price=None)
    manager.check_exit(position, _snapshot(10.4))  # +4%, below the 5% trigger
    assert position.stop_price == 9.5


def test_breakeven_never_loosens_an_already_tighter_stop():
    manager = PositionManager(PositionManagementConfig(breakeven_trigger_pct=5.0, trailing_stop_pct=None, exit_on_vwap_failure=False, time_limit_minutes=None))
    position = _position(stop_price=10.2, target_price=None)  # already above breakeven
    manager.check_exit(position, _snapshot(10.5))
    assert position.stop_price == 10.2  # unchanged, not pulled down to entry


def test_breakeven_disabled_when_trigger_pct_is_none():
    manager = PositionManager(PositionManagementConfig(breakeven_trigger_pct=None, trailing_stop_pct=None, exit_on_vwap_failure=False, time_limit_minutes=None))
    position = _position(stop_price=9.5, target_price=None)
    manager.check_exit(position, _snapshot(11.0))  # well past any trigger
    assert position.stop_price == 9.5


def test_trailing_does_not_apply_before_partial_exit_even_at_high_price():
    # Trailing is gated to only take over once a target has been hit (see
    # _maybe_update_trailing_stop's docstring) -- even at +10% with a 3%
    # trailing stop configured, an untouched position (no target, so
    # partial_exit_taken never flips True) stays at plain breakeven, not
    # the tighter trailing level.
    manager = PositionManager(PositionManagementConfig(breakeven_trigger_pct=5.0, trailing_stop_pct=3.0, exit_on_vwap_failure=False, time_limit_minutes=None))
    position = _position(stop_price=9.5, target_price=None)
    manager.check_exit(position, _snapshot(11.0))
    assert position.stop_price == 10.0  # breakeven only


def test_trailing_takes_over_once_partial_exit_taken():
    manager = PositionManager(PositionManagementConfig(breakeven_trigger_pct=5.0, trailing_stop_pct=3.0, exit_on_vwap_failure=False, time_limit_minutes=None))
    position = _position(stop_price=9.5, target_price=None, partial_exit_taken=True)
    manager.check_exit(position, _snapshot(11.0))
    assert position.stop_price == 11.0 * (1 - 3.0 / 100.0)


# -- target hit: partial exit -------------------------------------------------

def test_target_hit_emits_scale_out_when_quantity_sufficient():
    manager = PositionManager(PositionManagementConfig(trailing_stop_pct=None, exit_on_vwap_failure=False, time_limit_minutes=None, breakeven_trigger_pct=None))
    position = _position(quantity=10, target_price=11.0, stop_price=9.7)
    signal = manager.check_exit(position, _snapshot(11.1))
    assert signal is not None
    assert signal.action == SignalAction.SCALE_OUT
    assert signal.metadata["exit_reason"] == ExitReason.PARTIAL_PROFIT_TARGET.value


def test_target_hit_falls_back_to_full_exit_when_quantity_too_small_to_split():
    manager = PositionManager(PositionManagementConfig(trailing_stop_pct=None, exit_on_vwap_failure=False, time_limit_minutes=None, breakeven_trigger_pct=None))
    position = _position(quantity=1, target_price=11.0, stop_price=9.7)
    signal = manager.check_exit(position, _snapshot(11.1))
    assert signal is not None
    assert signal.action == SignalAction.EXIT
    assert signal.metadata["exit_reason"] == ExitReason.PROFIT_TARGET.value


def test_partial_exit_flag_prevents_retriggering_target():
    manager = PositionManager(PositionManagementConfig(trailing_stop_pct=None, exit_on_vwap_failure=False, time_limit_minutes=None, breakeven_trigger_pct=None))
    position = _position(quantity=10, target_price=11.0, stop_price=9.7, partial_exit_taken=True)
    signal = manager.check_exit(position, _snapshot(11.1))
    assert signal is None  # already took the partial; no target check left to fire


def test_no_signal_when_strategy_set_no_target():
    # VolumeIgnitionStrategy-style position: target_price is None, so no
    # partial or full target-hit exit is possible -- only stop/VWAP/time.
    manager = PositionManager(PositionManagementConfig(trailing_stop_pct=None, exit_on_vwap_failure=False, time_limit_minutes=None, breakeven_trigger_pct=None))
    position = _position(quantity=10, target_price=None, stop_price=9.7)
    signal = manager.check_exit(position, _snapshot(50.0))  # price way up, no target to hit
    assert signal is None


# -- other exit paths still emit full EXIT -----------------------------------

def test_stop_hit_emits_full_exit():
    manager = PositionManager(PositionManagementConfig(trailing_stop_pct=None, exit_on_vwap_failure=False, time_limit_minutes=None, breakeven_trigger_pct=None))
    position = _position(stop_price=9.7, target_price=11.0)
    signal = manager.check_exit(position, _snapshot(9.6))
    assert signal is not None
    assert signal.action == SignalAction.EXIT
    assert signal.metadata["exit_reason"] == ExitReason.STOP_LOSS.value


def test_stop_hit_reason_is_stop_loss_before_any_partial_exit():
    # Trailing hasn't taken over yet (no partial exit taken), so even with
    # trailing_stop_pct configured, a stop hit here is a plain stop loss --
    # see _maybe_update_trailing_stop's docstring for why trailing is gated
    # to only govern the stop after target is hit.
    manager = PositionManager(PositionManagementConfig(trailing_stop_pct=3.0, exit_on_vwap_failure=False, time_limit_minutes=None, breakeven_trigger_pct=None))
    position = _position(stop_price=9.7, target_price=None)
    signal = manager.check_exit(position, _snapshot(9.5))
    assert signal is not None
    assert signal.metadata["exit_reason"] == ExitReason.STOP_LOSS.value


def test_stop_hit_reason_is_trailing_stop_once_partial_exit_taken():
    manager = PositionManager(PositionManagementConfig(trailing_stop_pct=3.0, exit_on_vwap_failure=False, time_limit_minutes=None, breakeven_trigger_pct=None))
    position = _position(stop_price=9.7, target_price=None, partial_exit_taken=True)
    # Trailing recomputes to 10.5*0.97=10.185 on this tick (above the old
    # 9.7 stop), so a later drop to 10.1 catches the *trailing* level, not
    # the original stop.
    manager.check_exit(position, _snapshot(10.5))
    signal = manager.check_exit(position, _snapshot(10.1))
    assert signal is not None
    assert signal.metadata["exit_reason"] == ExitReason.TRAILING_STOP.value


def test_vwap_failure_emits_full_exit():
    manager = PositionManager(PositionManagementConfig(trailing_stop_pct=None, exit_on_vwap_failure=True, vwap_failure_buffer_pct=0.5, time_limit_minutes=None, breakeven_trigger_pct=None))
    position = _position(stop_price=8.0, target_price=None)  # stop far away so it doesn't fire first
    signal = manager.check_exit(position, _snapshot(9.9, vwap=10.0))  # 1% below VWAP
    assert signal is not None
    assert signal.action == SignalAction.EXIT
    assert signal.metadata["exit_reason"] == ExitReason.VWAP_FAILURE.value


def test_time_limit_emits_full_exit():
    manager = PositionManager(PositionManagementConfig(trailing_stop_pct=None, exit_on_vwap_failure=False, time_limit_minutes=30, breakeven_trigger_pct=None))
    position = _position(stop_price=8.0, target_price=None, opened_at=datetime.utcnow() - timedelta(minutes=31))
    signal = manager.check_exit(position, _snapshot(10.0))
    assert signal is not None
    assert signal.action == SignalAction.EXIT
    assert signal.metadata["exit_reason"] == ExitReason.TIME_LIMIT.value


def test_excursions_update_even_without_an_exit():
    manager = PositionManager(PositionManagementConfig(trailing_stop_pct=None, exit_on_vwap_failure=False, time_limit_minutes=None, breakeven_trigger_pct=None))
    position = _position(stop_price=8.0, target_price=None)
    signal = manager.check_exit(position, _snapshot(10.5))
    assert signal is None
    assert position.max_favorable_excursion == 5.0
