from datetime import datetime

from webull_bot.enums import CandidateState, SignalAction
from webull_bot.models import Candidate, MarketSnapshot, MomentumMetrics
from webull_bot.strategy.momentum_breakout import MomentumBreakoutConfig, MomentumBreakoutStrategy


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
                resistance_level=10.0, latest_metrics=_metrics())
    base.update(overrides)
    return Candidate(**base)


def _snapshot(price: float) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="ABCD", timestamp=datetime.utcnow(), last_price=price,
        bid=price - 0.01, ask=price + 0.01, bid_size=100, ask_size=100,
        cumulative_volume=100_000, vwap=10.0, high_of_day=price, low_of_day=9.0, open_price=9.5,
    )


def test_fires_when_price_clears_resistance_buffer():
    strategy = MomentumBreakoutStrategy()
    candidate = _candidate(resistance_level=10.0)
    signal = strategy.on_snapshot(candidate, _snapshot(10.02))  # > 10.0 * 1.001
    assert signal is not None
    assert signal.action == SignalAction.ENTER_LONG


def test_no_signal_below_breakout_buffer():
    strategy = MomentumBreakoutStrategy()
    candidate = _candidate(resistance_level=10.0)
    assert strategy.on_snapshot(candidate, _snapshot(10.0)) is None


def test_no_signal_without_resistance_level():
    strategy = MomentumBreakoutStrategy()
    candidate = _candidate(resistance_level=None)
    assert strategy.on_snapshot(candidate, _snapshot(10.5)) is None


def test_no_signal_when_volume_acceleration_too_low():
    strategy = MomentumBreakoutStrategy()
    candidate = _candidate(resistance_level=10.0, latest_metrics=_metrics(volume_accel_1m_3m=1.0))
    assert strategy.on_snapshot(candidate, _snapshot(10.5)) is None


def test_no_signal_when_not_armed():
    strategy = MomentumBreakoutStrategy()
    candidate = _candidate(state=CandidateState.WATCHING, resistance_level=10.0)
    assert strategy.on_snapshot(candidate, _snapshot(10.5)) is None


def test_stop_uses_flat_pct_when_it_is_tighter_than_the_structural_level():
    # rtms-v3-follow-up (2026-08-19, real incident: BTOG). A tight flat-pct
    # stop (1%) sits closer to entry than resistance_level -- the flat %
    # is the risk ceiling the user configured, so it wins (max(), not
    # min()) rather than the strategy widening the stop out to resistance.
    strategy = MomentumBreakoutStrategy(stop_loss_pct_fn=lambda: 1.0)
    candidate = _candidate(resistance_level=10.0)
    signal = strategy.on_snapshot(candidate, _snapshot(10.5))
    assert signal is not None
    assert signal.suggested_stop == 10.5 * 0.99


def test_stop_uses_structural_level_when_it_is_tighter_than_flat_pct():
    # resistance_level (10.0) sits closer to entry (10.5) than the loose
    # 10%-flat-pct stop (9.45) would -- the structural level tightens the
    # stop below the risk ceiling, exactly the intended "stop tucked under
    # the broken resistance" behavior.
    strategy = MomentumBreakoutStrategy(stop_loss_pct_fn=lambda: 10.0)
    candidate = _candidate(resistance_level=10.0)
    signal = strategy.on_snapshot(candidate, _snapshot(10.5))
    assert signal is not None
    assert signal.suggested_stop == 10.0


def test_stop_capped_at_flat_pct_when_structural_level_is_far_below_entry():
    # Direct BTOG-shaped regression: resistance_level is stale/far below
    # entry (well past what stop_loss_pct alone would ever allow, e.g. a
    # low-float runner that already ran well past its old resistance) --
    # the final stop must be capped at the flat-% ceiling, not the far
    # structural level, and the signal must still fire (not rejected).
    strategy = MomentumBreakoutStrategy(stop_loss_pct_fn=lambda: 5.0)
    candidate = _candidate(resistance_level=7.7)  # far below the 10.5 entry
    signal = strategy.on_snapshot(candidate, _snapshot(10.5))
    assert signal is not None
    assert signal.suggested_stop == 10.5 * 0.95
    assert signal.suggested_stop > candidate.resistance_level


def test_target_follows_injected_reward_risk_ratio():
    strategy = MomentumBreakoutStrategy(reward_risk_ratio_fn=lambda: 3.0)
    candidate = _candidate(resistance_level=10.0)
    signal = strategy.on_snapshot(candidate, _snapshot(10.5))
    assert signal is not None
    risk_per_share = signal.reference_price - signal.suggested_stop
    assert signal.suggested_target == signal.reference_price + risk_per_share * 3.0
