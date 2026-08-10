from datetime import datetime

from webull_bot.enums import CandidateState, SignalAction
from webull_bot.models import Candidate, MarketSnapshot, MomentumMetrics
from webull_bot.strategy.ignition_pullback import IgnitionPullbackConfig, IgnitionPullbackStrategy


def _metrics(**overrides) -> MomentumMetrics:
    base = dict(
        symbol="ABCD", timestamp=datetime.utcnow(),
        float_turnover=0.1, float_velocity_1m=0.01, float_velocity_3m=0.02, float_velocity_5m=0.01,
        relative_volume=1.0, relative_volume_1m=1.0, relative_volume_5m=1.0,
        volume_accel_1m_3m=4.0,
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
    base = dict(symbol="ABCD", state=CandidateState.ARMED, discovered_at=now, last_updated_at=now)
    base.update(overrides)
    return Candidate(**base)


def _snapshot(price: float) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="ABCD", timestamp=datetime.utcnow(), last_price=price,
        bid=price - 0.01, ask=price + 0.01, bid_size=100, ask_size=100,
        cumulative_volume=100_000, vwap=10.0, high_of_day=price, low_of_day=9.0, open_price=9.5,
    )


def test_full_ignition_pullback_reclaim_sequence_fires():
    strategy = IgnitionPullbackStrategy()
    candidate = _candidate()

    # Ignition: volume+price surge above VWAP.
    candidate.latest_metrics = _metrics(volume_accel_1m_3m=4.0, price_velocity_1m=1.0, distance_from_vwap_pct=1.0)
    assert strategy.on_snapshot(candidate, _snapshot(12.0)) is None

    # Pullback: price dips, volume declines.
    candidate.latest_metrics = _metrics(volume_accel_1m_3m=0.5)
    assert strategy.on_snapshot(candidate, _snapshot(11.5)) is None

    # Reclaim: price ticks back up off the pullback low with volume returning -- arms READY_TO_ENTER.
    candidate.latest_metrics = _metrics(volume_accel_1m_3m=1.5)
    assert strategy.on_snapshot(candidate, _snapshot(11.6)) is None

    # Entry: price clears reclaim buffer above pullback low.
    signal = strategy.on_snapshot(candidate, _snapshot(11.7))
    assert signal is not None
    assert signal.action == SignalAction.ENTER_LONG
    assert signal.suggested_stop < signal.reference_price


def test_no_ignition_means_no_progress():
    strategy = IgnitionPullbackStrategy()
    candidate = _candidate(latest_metrics=_metrics(volume_accel_1m_3m=1.0, float_velocity_5m=0.01))
    assert strategy.on_snapshot(candidate, _snapshot(12.0)) is None


def test_deep_pullback_invalidates_and_resets():
    strategy = IgnitionPullbackStrategy(IgnitionPullbackConfig(max_pullback_retrace_pct=20.0))
    candidate = _candidate()
    candidate.latest_metrics = _metrics(volume_accel_1m_3m=4.0)
    strategy.on_snapshot(candidate, _snapshot(12.0))

    # Pulls back more than 20% of the ignition price -- invalidates.
    candidate.latest_metrics = _metrics(volume_accel_1m_3m=0.5)
    assert strategy.on_snapshot(candidate, _snapshot(9.0)) is None
    assert strategy._phase["ABCD"].value == "awaiting_ignition"


def test_no_signal_when_not_armed():
    strategy = IgnitionPullbackStrategy()
    candidate = _candidate(state=CandidateState.WATCHING, latest_metrics=_metrics(volume_accel_1m_3m=4.0))
    assert strategy.on_snapshot(candidate, _snapshot(12.0)) is None


def test_no_signal_without_metrics():
    strategy = IgnitionPullbackStrategy()
    candidate = _candidate(latest_metrics=None)
    assert strategy.on_snapshot(candidate, _snapshot(12.0)) is None
