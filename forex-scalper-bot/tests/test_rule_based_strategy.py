from datetime import datetime

import pytest

from fx_bot.enums import OrderSide, SignalAction
from fx_bot.models import MarketSnapshot, Position
from fx_bot.strategy_builder.compiler import compile
from fx_bot.strategy_builder.schema import StrategyConfig


def _bars(mids: list[float]) -> list[MarketSnapshot]:
    return [
        MarketSnapshot(symbol="EUR/USD", timestamp=datetime(2026, 1, 1, 0, i, 0), bid=m - 0.0001, ask=m + 0.0001)
        for i, m in enumerate(mids)
    ]


def _ema_cross_long_config(**overrides) -> StrategyConfig:
    base = dict(
        id="t1", name="ema_cross", pair="EUR/USD", entry_side="long",
        indicators=[
            {"id": "fast", "type": "ema", "params": {"period": 2}},
            {"id": "slow", "type": "ema", "params": {"period": 4}},
        ],
        entry_conditions={
            "op": "and",
            "items": [{
                "left": {"kind": "indicator", "indicator_id": "fast"},
                "operator": "crosses_above",
                "right": {"kind": "indicator", "indicator_id": "slow"},
            }],
        },
        stop_loss={"pips": 10},
        take_profit={"type": "fixed_pips", "pips": 20},
    )
    base.update(overrides)
    return StrategyConfig(**base)


def test_wrong_symbol_is_ignored():
    strategy = compile(_ema_cross_long_config())
    bars = _bars([1.10, 1.10, 1.10, 1.10, 1.11])
    signal = strategy.on_snapshot("GBP/USD", bars[-1], bars, position=None)
    assert signal is None


def test_no_signal_while_indicators_lack_enough_history():
    strategy = compile(_ema_cross_long_config())
    bars = _bars([1.10, 1.10])  # far short of the slow EMA's period=4
    signal = strategy.on_snapshot("EUR/USD", bars[-1], bars, position=None)
    assert signal is None


def test_enters_long_on_a_genuine_crossover_and_sets_pip_based_stop_and_target():
    strategy = compile(_ema_cross_long_config())
    # A falling-then-rising series so the fast EMA(2) crosses above the
    # slow EMA(4) at some point once enough bars exist.
    bars = _bars([1.1050, 1.1040, 1.1030, 1.1020, 1.1010, 1.1050, 1.1090, 1.1130])

    signal = None
    for i in range(4, len(bars)):
        signal = strategy.on_snapshot("EUR/USD", bars[i], bars[: i + 1], position=None)
        if signal is not None:
            break

    assert signal is not None
    assert signal.action == SignalAction.ENTER_LONG
    assert signal.suggested_stop == pytest.approx(signal.reference_price - 0.0010)
    assert signal.suggested_target == pytest.approx(signal.reference_price + 0.0020)


def test_short_entry_computes_stop_and_target_on_the_opposite_sides():
    config = _ema_cross_long_config(entry_side="short", entry_conditions={
        "op": "and",
        "items": [{
            "left": {"kind": "indicator", "indicator_id": "fast"},
            "operator": "crosses_below",
            "right": {"kind": "indicator", "indicator_id": "slow"},
        }],
    })
    strategy = compile(config)
    bars = _bars([1.1010, 1.1020, 1.1030, 1.1040, 1.1050, 1.1010, 1.0970, 1.0930])

    signal = None
    for i in range(4, len(bars)):
        signal = strategy.on_snapshot("EUR/USD", bars[i], bars[: i + 1], position=None)
        if signal is not None:
            break

    assert signal is not None
    assert signal.action == SignalAction.ENTER_SHORT
    assert signal.suggested_stop == pytest.approx(signal.reference_price + 0.0010)
    assert signal.suggested_target == pytest.approx(signal.reference_price - 0.0020)


def test_risk_reward_ratio_take_profit_scales_off_the_stop_distance():
    config = _ema_cross_long_config(
        stop_loss={"pips": 10}, take_profit={"type": "risk_reward_ratio", "ratio": 3.0},
    )
    strategy = compile(config)
    bars = _bars([1.1050, 1.1040, 1.1030, 1.1020, 1.1010, 1.1050, 1.1090, 1.1130])

    signal = None
    for i in range(4, len(bars)):
        signal = strategy.on_snapshot("EUR/USD", bars[i], bars[: i + 1], position=None)
        if signal is not None:
            break

    assert signal is not None
    stop_distance = signal.reference_price - signal.suggested_stop
    target_distance = signal.suggested_target - signal.reference_price
    assert target_distance == pytest.approx(stop_distance * 3.0)


def test_never_re_enters_while_a_position_is_already_open():
    strategy = compile(_ema_cross_long_config())
    bars = _bars([1.1050, 1.1040, 1.1030, 1.1020, 1.1010, 1.1050, 1.1090, 1.1130])
    position = Position(
        symbol="EUR/USD", side=OrderSide.BUY, quantity=10_000, avg_entry_price=1.1000,
        stop_price=1.0990, target_price=1.1020, trailing_stop_pips=None,
        opened_at=datetime.utcnow(), strategy_name="ema_cross",
    )
    for i in range(4, len(bars)):
        signal = strategy.on_snapshot("EUR/USD", bars[i], bars[: i + 1], position=position)
        assert signal is None  # no exit_conditions configured -> never exits either


def test_exits_when_exit_conditions_are_configured_and_satisfied():
    config = _ema_cross_long_config(exit_conditions={
        "op": "and",
        "items": [{
            "left": {"kind": "indicator", "indicator_id": "fast"},
            "operator": "crosses_below",
            "right": {"kind": "indicator", "indicator_id": "slow"},
        }],
    })
    strategy = compile(config)
    # Rising then falling -- fast EMA(2) will cross back below slow EMA(4).
    bars = _bars([1.1010, 1.1020, 1.1030, 1.1040, 1.1080, 1.1120, 1.1080, 1.1040, 1.1000, 1.0960])
    position = Position(
        symbol="EUR/USD", side=OrderSide.BUY, quantity=10_000, avg_entry_price=1.1050,
        stop_price=1.1040, target_price=1.1070, trailing_stop_pips=None,
        opened_at=datetime.utcnow(), strategy_name="ema_cross",
    )

    signal = None
    for i in range(4, len(bars)):
        signal = strategy.on_snapshot("EUR/USD", bars[i], bars[: i + 1], position=position)
        if signal is not None:
            break

    assert signal is not None
    assert signal.action == SignalAction.EXIT
