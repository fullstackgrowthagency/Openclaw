from datetime import datetime

from webull_bot.models import MarketSnapshot, MomentumMetrics
from webull_bot.scanner.momentum_structure import momentum_structure_intact
from webull_bot.state_machine import new_candidate

_NOW = datetime(2026, 8, 17, 15, 0, 0)


def _snapshot(last_price: float, vwap: float = 10.0) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="TEST", timestamp=_NOW, last_price=last_price, bid=last_price - 0.01, ask=last_price + 0.01,
        bid_size=100, ask_size=100, cumulative_volume=100_000, vwap=vwap, high_of_day=last_price,
        low_of_day=last_price - 1.0, open_price=last_price - 0.5,
    )


def _candidate():
    return new_candidate("TEST", now=_NOW)


def test_breakout_strategy_intact_when_price_holds_resistance():
    candidate = _candidate()
    candidate.resistance_level = 10.0
    assert momentum_structure_intact(candidate, "momentum_breakout", _snapshot(10.05)) is True


def test_breakout_strategy_broken_when_price_falls_meaningfully_below_resistance():
    candidate = _candidate()
    candidate.resistance_level = 10.0
    assert momentum_structure_intact(candidate, "momentum_breakout", _snapshot(9.5)) is False


def test_breakout_strategy_fails_open_when_no_resistance_known():
    candidate = _candidate()
    candidate.resistance_level = None
    assert momentum_structure_intact(candidate, "refined_breakout", _snapshot(9.0)) is True


def test_tolerance_pct_allows_a_tiny_undershoot():
    candidate = _candidate()
    candidate.resistance_level = 10.0
    # 0.1% below -- inside the default 0.15% tolerance.
    assert momentum_structure_intact(candidate, "momentum_breakout", _snapshot(9.99)) is True


def test_opening_range_breakout_checks_orb_high():
    candidate = _candidate()
    candidate.opening_range_high = 10.0
    assert momentum_structure_intact(candidate, "opening_range_breakout", _snapshot(9.0)) is False
    assert momentum_structure_intact(candidate, "opening_range_breakout", _snapshot(10.0)) is True


def test_vwap_reclaim_checks_vwap():
    candidate = _candidate()
    candidate.latest_metrics = None
    assert momentum_structure_intact(candidate, "vwap_reclaim", _snapshot(9.0, vwap=10.0)) is True  # no metrics -> fails open


def test_pullback_strategies_check_qualification_layers_own_pullback_low():
    candidate = _candidate()
    candidate.momentum.pullback_low = 9.5
    assert momentum_structure_intact(candidate, "breakout_pullback", _snapshot(9.6)) is True
    assert momentum_structure_intact(candidate, "ignition_pullback", _snapshot(9.0)) is False


def test_volume_ignition_falls_back_to_vwap_held():
    candidate = _candidate()
    candidate.latest_metrics = MomentumMetrics(
        symbol="TEST", timestamp=_NOW, float_turnover=0.1, float_velocity_1m=0.0, float_velocity_3m=0.0,
        float_velocity_5m=0.0, relative_volume=1.0, relative_volume_1m=1.0, relative_volume_5m=1.0,
        volume_accel_1m_3m=1.0, volume_1m=0.0, volume_5m=0.0, volume_15m=0.0, dollar_volume_1m=0.0,
        dollar_volume_5m=0.0, dollar_volume_15m=0.0, dollar_volume_accel_1m_3m=1.0, price_velocity_1m=0.0,
        price_velocity_3m=0.0, price_velocity_5m=0.0, price_velocity_15m=0.0, price_acceleration=0.0,
        vwap=10.0, distance_from_vwap_pct=0.0, distance_from_hod_pct=0.0, distance_from_premarket_high_pct=None,
        distance_from_resistance_pct=None, spread_abs=0.01, spread_pct=0.1, dollar_volume=1_000_000,
    )
    assert momentum_structure_intact(candidate, "volume_ignition", _snapshot(10.5, vwap=10.0)) is True
    assert momentum_structure_intact(candidate, "volume_ignition", _snapshot(9.5, vwap=10.0)) is False
