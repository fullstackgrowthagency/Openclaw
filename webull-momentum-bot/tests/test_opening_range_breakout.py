from datetime import datetime

from webull_bot.enums import CandidateState, SignalAction
from webull_bot.models import Candidate, MarketSnapshot, MomentumMetrics
from webull_bot.strategy.opening_range_breakout import OpeningRangeBreakoutConfig, OpeningRangeBreakoutStrategy


def _metrics(**overrides) -> MomentumMetrics:
    base = dict(
        symbol="ABCD", timestamp=datetime.utcnow(),
        float_turnover=0.1, float_velocity_1m=0.01, float_velocity_3m=0.02, float_velocity_5m=0.01,
        relative_volume=1.0, relative_volume_1m=1.0, relative_volume_5m=1.0,
        volume_accel_1m_3m=2.0,
        volume_1m=0.0, volume_5m=0.0, volume_15m=0.0,
        dollar_volume_1m=0.0, dollar_volume_5m=0.0, dollar_volume_15m=0.0, dollar_volume_accel_1m_3m=1.0,
        price_velocity_1m=0.0, price_velocity_3m=0.0, price_velocity_5m=0.0, price_velocity_15m=0.0,
        price_acceleration=0.0,
        vwap=10.0, distance_from_vwap_pct=0.0, distance_from_hod_pct=0.0,
        distance_from_premarket_high_pct=None, distance_from_resistance_pct=None,
        spread_abs=0.01, spread_pct=0.1, dollar_volume=1_000_000,
    )
    base.update(overrides)
    return MomentumMetrics(**base)


def _candidate(**overrides) -> Candidate:
    now = datetime.utcnow()
    base = dict(symbol="ABCD", state=CandidateState.ARMED, discovered_at=now, last_updated_at=now,
                opening_range_high=10.0, latest_metrics=_metrics())
    base.update(overrides)
    return Candidate(**base)


def _snapshot(price: float) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="ABCD", timestamp=datetime.utcnow(), last_price=price,
        bid=price - 0.01, ask=price + 0.01, bid_size=100, ask_size=100,
        cumulative_volume=100_000, vwap=10.0, high_of_day=price, low_of_day=9.0, open_price=9.5,
    )


def test_fires_when_price_clears_opening_range_high_buffer():
    strategy = OpeningRangeBreakoutStrategy()
    candidate = _candidate(opening_range_high=10.0)
    signal = strategy.on_snapshot(candidate, _snapshot(10.02))  # > 10.0 * 1.001
    assert signal is not None
    assert signal.action == SignalAction.ENTER_LONG


def test_no_signal_below_breakout_buffer():
    strategy = OpeningRangeBreakoutStrategy()
    candidate = _candidate(opening_range_high=10.0)
    assert strategy.on_snapshot(candidate, _snapshot(10.0)) is None


def test_no_signal_without_opening_range_high():
    strategy = OpeningRangeBreakoutStrategy()
    candidate = _candidate(opening_range_high=None)
    assert strategy.on_snapshot(candidate, _snapshot(10.5)) is None


def test_no_signal_when_volume_acceleration_too_low():
    strategy = OpeningRangeBreakoutStrategy()
    candidate = _candidate(opening_range_high=10.0, latest_metrics=_metrics(volume_accel_1m_3m=1.0))
    assert strategy.on_snapshot(candidate, _snapshot(10.5)) is None


def test_stop_clamped_to_opening_range_high_when_flat_pct_would_sit_above_it():
    # A very tight flat-pct stop (1%) would land above opening_range_high --
    # the strategy clamps down to opening_range_high as a structural floor.
    strategy = OpeningRangeBreakoutStrategy(OpeningRangeBreakoutConfig(initial_stop_pct=1.0))
    candidate = _candidate(opening_range_high=10.0)
    signal = strategy.on_snapshot(candidate, _snapshot(10.5))
    assert signal is not None
    assert signal.suggested_stop == 10.0


def test_stop_uses_flat_pct_when_it_sits_below_opening_range_high():
    strategy = OpeningRangeBreakoutStrategy(OpeningRangeBreakoutConfig(initial_stop_pct=10.0))
    candidate = _candidate(opening_range_high=10.0)
    signal = strategy.on_snapshot(candidate, _snapshot(10.5))
    assert signal is not None
    assert signal.suggested_stop == 10.5 * 0.9


def test_no_signal_when_not_armed():
    strategy = OpeningRangeBreakoutStrategy()
    candidate = _candidate(state=CandidateState.WATCHING, opening_range_high=10.0)
    assert strategy.on_snapshot(candidate, _snapshot(10.5)) is None


def test_target_follows_injected_reward_risk_ratio():
    strategy = OpeningRangeBreakoutStrategy(reward_risk_ratio_fn=lambda: 3.0)
    candidate = _candidate(opening_range_high=10.0)
    signal = strategy.on_snapshot(candidate, _snapshot(10.5))
    assert signal is not None
    risk_per_share = signal.reference_price - signal.suggested_stop
    assert signal.suggested_target == signal.reference_price + risk_per_share * 3.0
