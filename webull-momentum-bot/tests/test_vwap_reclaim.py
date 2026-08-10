from datetime import datetime

from webull_bot.enums import CandidateState, SignalAction
from webull_bot.models import Candidate, MarketSnapshot, MomentumMetrics
from webull_bot.strategy.vwap_reclaim import VWAPReclaimConfig, VWAPReclaimStrategy


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
    base = dict(symbol="ABCD", state=CandidateState.ARMED, discovered_at=now, last_updated_at=now)
    base.update(overrides)
    return Candidate(**base)


def _snapshot(price: float = 10.0) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="ABCD", timestamp=datetime.utcnow(), last_price=price,
        bid=price - 0.01, ask=price + 0.01, bid_size=100, ask_size=100,
        cumulative_volume=100_000, vwap=10.0, high_of_day=price, low_of_day=9.0, open_price=9.5,
    )


def test_no_signal_on_first_dip_below_vwap():
    strategy = VWAPReclaimStrategy()
    candidate = _candidate(latest_metrics=_metrics(distance_from_vwap_pct=-0.5))
    assert strategy.on_snapshot(candidate, _snapshot()) is None


def test_fires_on_reclaim_after_dip():
    strategy = VWAPReclaimStrategy()
    candidate = _candidate()
    # First tick: dips below VWAP -- arms the "was below" flag.
    candidate.latest_metrics = _metrics(distance_from_vwap_pct=-0.5)
    assert strategy.on_snapshot(candidate, _snapshot()) is None
    # Second tick: reclaims VWAP with volume.
    candidate.latest_metrics = _metrics(distance_from_vwap_pct=0.5, volume_accel_1m_3m=2.0)
    signal = strategy.on_snapshot(candidate, _snapshot(10.1))
    assert signal is not None
    assert signal.action == SignalAction.ENTER_LONG
    assert signal.suggested_stop < signal.reference_price


def test_no_signal_without_prior_dip():
    strategy = VWAPReclaimStrategy()
    candidate = _candidate(latest_metrics=_metrics(distance_from_vwap_pct=0.5, volume_accel_1m_3m=2.0))
    assert strategy.on_snapshot(candidate, _snapshot()) is None


def test_no_signal_when_reclaim_buffer_not_cleared():
    strategy = VWAPReclaimStrategy()
    candidate = _candidate()
    candidate.latest_metrics = _metrics(distance_from_vwap_pct=-0.5)
    strategy.on_snapshot(candidate, _snapshot())
    candidate.latest_metrics = _metrics(distance_from_vwap_pct=0.01, volume_accel_1m_3m=2.0)
    assert strategy.on_snapshot(candidate, _snapshot()) is None


def test_no_signal_when_volume_acceleration_too_low_on_reclaim():
    strategy = VWAPReclaimStrategy()
    candidate = _candidate()
    candidate.latest_metrics = _metrics(distance_from_vwap_pct=-0.5)
    strategy.on_snapshot(candidate, _snapshot())
    candidate.latest_metrics = _metrics(distance_from_vwap_pct=0.5, volume_accel_1m_3m=1.0)
    assert strategy.on_snapshot(candidate, _snapshot()) is None


def test_reclaim_resets_and_requires_fresh_dip():
    strategy = VWAPReclaimStrategy()
    candidate = _candidate()
    candidate.latest_metrics = _metrics(distance_from_vwap_pct=-0.5)
    strategy.on_snapshot(candidate, _snapshot())
    candidate.latest_metrics = _metrics(distance_from_vwap_pct=0.5, volume_accel_1m_3m=2.0)
    first = strategy.on_snapshot(candidate, _snapshot(10.1))
    assert first is not None
    # Still above VWAP -- should not immediately fire again without a fresh dip.
    second = strategy.on_snapshot(candidate, _snapshot(10.2))
    assert second is None


def test_no_signal_when_not_armed():
    strategy = VWAPReclaimStrategy()
    candidate = _candidate(state=CandidateState.WATCHING, latest_metrics=_metrics(distance_from_vwap_pct=-0.5))
    assert strategy.on_snapshot(candidate, _snapshot()) is None


def test_custom_below_vwap_threshold_respected():
    strategy = VWAPReclaimStrategy(VWAPReclaimConfig(below_vwap_threshold_pct=-1.0))
    candidate = _candidate(latest_metrics=_metrics(distance_from_vwap_pct=-0.5))
    # -0.5% doesn't clear the stricter -1.0% threshold -- shouldn't arm.
    strategy.on_snapshot(candidate, _snapshot())
    candidate.latest_metrics = _metrics(distance_from_vwap_pct=0.5, volume_accel_1m_3m=2.0)
    assert strategy.on_snapshot(candidate, _snapshot()) is None


def test_target_follows_injected_reward_risk_ratio():
    strategy = VWAPReclaimStrategy(reward_risk_ratio_fn=lambda: 3.0)
    candidate = _candidate()
    candidate.latest_metrics = _metrics(distance_from_vwap_pct=-0.5)
    strategy.on_snapshot(candidate, _snapshot())
    candidate.latest_metrics = _metrics(distance_from_vwap_pct=0.5, volume_accel_1m_3m=2.0)
    signal = strategy.on_snapshot(candidate, _snapshot(10.1))
    assert signal is not None
    risk_per_share = signal.reference_price - signal.suggested_stop
    assert signal.suggested_target == signal.reference_price + risk_per_share * 3.0
