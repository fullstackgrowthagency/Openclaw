from datetime import datetime

from webull_bot.enums import CandidateState, SignalAction
from webull_bot.models import Candidate, MarketSnapshot, MomentumMetrics
from webull_bot.strategy.volatility_contraction import (
    VolatilityContractionBreakoutStrategy,
    VolatilityContractionConfig,
)


def _metrics(**overrides) -> MomentumMetrics:
    base = dict(
        symbol="ABCD", timestamp=datetime.utcnow(),
        float_turnover=0.1, float_velocity_1m=0.01, float_velocity_3m=0.02, float_velocity_5m=0.01,
        relative_volume=1.0, relative_volume_1m=1.0, relative_volume_5m=1.0,
        volume_accel_1m_3m=3.0,
        volume_1m=0.0, volume_5m=0.0, volume_15m=0.0,
        dollar_volume_1m=0.0, dollar_volume_5m=0.0, dollar_volume_15m=0.0, dollar_volume_accel_1m_3m=1.0,
        price_velocity_1m=1.0, price_velocity_3m=0.0, price_velocity_5m=0.0, price_velocity_15m=0.0,
        price_acceleration=0.0,
        vwap=10.0, distance_from_vwap_pct=0.0, distance_from_hod_pct=0.0,
        distance_from_premarket_high_pct=None, distance_from_resistance_pct=None,
        spread_abs=0.01, spread_pct=0.1, dollar_volume=1_000_000,
        price_range_pct_3m=1.0, price_range_pct_15m=5.0,
    )
    base.update(overrides)
    return MomentumMetrics(**base)


def _candidate(**overrides) -> Candidate:
    now = datetime.utcnow()
    base = dict(symbol="ABCD", state=CandidateState.ARMED, discovered_at=now, last_updated_at=now,
                latest_metrics=_metrics())
    base.update(overrides)
    return Candidate(**base)


def _snapshot(price: float = 10.0) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="ABCD", timestamp=datetime.utcnow(), last_price=price,
        bid=price - 0.01, ask=price + 0.01, bid_size=100, ask_size=100,
        cumulative_volume=100_000, vwap=10.0, high_of_day=price, low_of_day=9.0, open_price=9.5,
    )


def test_fires_on_tight_range_expanding_with_volume():
    strategy = VolatilityContractionBreakoutStrategy()
    candidate = _candidate(latest_metrics=_metrics(price_range_pct_3m=1.0, price_range_pct_15m=5.0))
    signal = strategy.on_snapshot(candidate, _snapshot())
    assert signal is not None
    assert signal.action == SignalAction.ENTER_LONG
    assert signal.metadata["contraction_ratio"] == 0.2


def test_no_signal_when_not_contracted_enough():
    strategy = VolatilityContractionBreakoutStrategy()
    candidate = _candidate(latest_metrics=_metrics(price_range_pct_3m=4.0, price_range_pct_15m=5.0))
    assert strategy.on_snapshot(candidate, _snapshot()) is None


def test_no_signal_when_generally_quiet_not_contracting():
    strategy = VolatilityContractionBreakoutStrategy()
    # Both windows tiny -- ratio might be low, but 15m range itself is too small to count as a real prior move.
    candidate = _candidate(latest_metrics=_metrics(price_range_pct_3m=0.05, price_range_pct_15m=0.2))
    assert strategy.on_snapshot(candidate, _snapshot()) is None


def test_no_signal_without_volume_confirmation():
    strategy = VolatilityContractionBreakoutStrategy()
    candidate = _candidate(latest_metrics=_metrics(price_range_pct_3m=1.0, price_range_pct_15m=5.0, volume_accel_1m_3m=1.0))
    assert strategy.on_snapshot(candidate, _snapshot()) is None


def test_no_signal_when_price_falling():
    strategy = VolatilityContractionBreakoutStrategy()
    candidate = _candidate(latest_metrics=_metrics(price_range_pct_3m=1.0, price_range_pct_15m=5.0, price_velocity_1m=-1.0))
    assert strategy.on_snapshot(candidate, _snapshot()) is None


def test_custom_thresholds_respected():
    config = VolatilityContractionConfig(max_contraction_ratio=0.1, min_broader_range_pct=1.0)
    strategy = VolatilityContractionBreakoutStrategy(config)
    candidate = _candidate(latest_metrics=_metrics(price_range_pct_3m=1.0, price_range_pct_15m=5.0))  # ratio 0.2 > 0.1
    assert strategy.on_snapshot(candidate, _snapshot()) is None


def test_no_signal_when_not_armed():
    strategy = VolatilityContractionBreakoutStrategy()
    candidate = _candidate(state=CandidateState.WATCHING)
    assert strategy.on_snapshot(candidate, _snapshot()) is None


def test_target_follows_injected_reward_risk_ratio():
    strategy = VolatilityContractionBreakoutStrategy(reward_risk_ratio_fn=lambda: 3.0)
    candidate = _candidate(latest_metrics=_metrics(price_range_pct_3m=1.0, price_range_pct_15m=5.0))
    signal = strategy.on_snapshot(candidate, _snapshot())
    assert signal is not None
    risk_per_share = signal.reference_price - signal.suggested_stop
    assert signal.suggested_target == signal.reference_price + risk_per_share * 3.0
