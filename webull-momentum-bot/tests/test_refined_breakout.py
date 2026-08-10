from datetime import datetime

from webull_bot.enums import CandidateState, SignalAction
from webull_bot.models import Candidate, MarketSnapshot, MomentumMetrics
from webull_bot.strategy.refined_breakout import RefinedBreakoutConfig, RefinedBreakoutStrategy


def _metrics(**overrides) -> MomentumMetrics:
    base = dict(
        symbol="ABCD", timestamp=datetime.utcnow(),
        float_turnover=0.1, float_velocity_1m=0.01, float_velocity_3m=0.02, float_velocity_5m=0.01,
        relative_volume=1.0, relative_volume_1m=1.0, relative_volume_5m=1.0,
        volume_accel_1m_3m=2.0,
        volume_1m=0.0, volume_5m=0.0, volume_15m=0.0,
        dollar_volume_1m=0.0, dollar_volume_5m=0.0, dollar_volume_15m=0.0, dollar_volume_accel_1m_3m=1.0,
        price_velocity_1m=1.0, price_velocity_3m=0.0, price_velocity_5m=0.0, price_velocity_15m=0.0,
        price_acceleration=0.0,
        vwap=10.0, distance_from_vwap_pct=1.0, distance_from_hod_pct=0.0,
        distance_from_premarket_high_pct=None, distance_from_resistance_pct=None,
        spread_abs=0.01, spread_pct=0.1, dollar_volume=1_000_000,
    )
    base.update(overrides)
    return MomentumMetrics(**base)


def _candidate(**overrides) -> Candidate:
    now = datetime.utcnow()
    base = dict(symbol="ABCD", state=CandidateState.ARMED, discovered_at=now, last_updated_at=now,
                resistance_level=10.0, latest_metrics=_metrics())
    base.update(overrides)
    return Candidate(**base)


def _snapshot(price: float) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="ABCD", timestamp=datetime.utcnow(), last_price=price,
        bid=price - 0.01, ask=price + 0.01, bid_size=100, ask_size=100,
        cumulative_volume=100_000, vwap=10.0, high_of_day=price, low_of_day=9.0, open_price=9.5,
    )


def test_fires_within_3pct_buffer_above_resistance():
    strategy = RefinedBreakoutStrategy()
    candidate = _candidate(resistance_level=10.0)
    signal = strategy.on_snapshot(candidate, _snapshot(10.2))  # 2% above resistance
    assert signal is not None
    assert signal.action == SignalAction.ENTER_LONG


def test_fires_exactly_at_resistance():
    strategy = RefinedBreakoutStrategy()
    candidate = _candidate(resistance_level=10.0)
    signal = strategy.on_snapshot(candidate, _snapshot(10.0))
    assert signal is not None


def test_fires_exactly_at_3pct_boundary():
    strategy = RefinedBreakoutStrategy()
    candidate = _candidate(resistance_level=10.0)
    signal = strategy.on_snapshot(candidate, _snapshot(10.30))
    assert signal is not None


def test_no_signal_beyond_3pct_buffer():
    strategy = RefinedBreakoutStrategy()
    candidate = _candidate(resistance_level=10.0)
    signal = strategy.on_snapshot(candidate, _snapshot(10.5))  # 5% above resistance -- too extended
    assert signal is None


def test_no_signal_below_resistance():
    strategy = RefinedBreakoutStrategy()
    candidate = _candidate(resistance_level=10.0)
    signal = strategy.on_snapshot(candidate, _snapshot(9.9))
    assert signal is None


def test_custom_buffer_pct_respected():
    strategy = RefinedBreakoutStrategy(RefinedBreakoutConfig(max_breakout_extension_pct=5.0))
    candidate = _candidate(resistance_level=10.0)
    assert strategy.on_snapshot(candidate, _snapshot(10.4)) is not None


def test_no_signal_without_resistance_level():
    strategy = RefinedBreakoutStrategy()
    candidate = _candidate(resistance_level=None)
    assert strategy.on_snapshot(candidate, _snapshot(10.2)) is None


def test_no_signal_when_volume_acceleration_too_low():
    strategy = RefinedBreakoutStrategy()
    candidate = _candidate(resistance_level=10.0, latest_metrics=_metrics(volume_accel_1m_3m=1.0))
    assert strategy.on_snapshot(candidate, _snapshot(10.2)) is None


def test_no_signal_when_not_armed():
    strategy = RefinedBreakoutStrategy()
    candidate = _candidate(state=CandidateState.WATCHING, resistance_level=10.0)
    assert strategy.on_snapshot(candidate, _snapshot(10.2)) is None
