from datetime import datetime

from webull_bot.enums import CandidateState, SignalAction
from webull_bot.models import Candidate, MarketSnapshot, MomentumMetrics
from webull_bot.strategy.volume_ignition import VolumeIgnitionConfig, VolumeIgnitionStrategy


def _metrics(**overrides) -> MomentumMetrics:
    base = dict(
        symbol="ABCD",
        timestamp=datetime.utcnow(),
        float_turnover=0.1,
        float_velocity_1m=0.01,
        float_velocity_3m=0.02,
        float_velocity_5m=0.01,
        relative_volume=1.0,
        relative_volume_1m=1.0,
        relative_volume_5m=1.0,
        volume_accel_1m_3m=4.0,
        volume_1m=0.0,
        volume_5m=0.0,
        volume_15m=0.0,
        dollar_volume_1m=0.0,
        dollar_volume_5m=0.0,
        dollar_volume_15m=0.0,
        dollar_volume_accel_1m_3m=1.0,
        price_velocity_1m=1.0,
        price_velocity_3m=0.0,
        price_velocity_5m=0.0,
        price_velocity_15m=0.0,
        price_acceleration=0.0,
        vwap=10.0,
        distance_from_vwap_pct=1.0,
        distance_from_hod_pct=0.0,
        distance_from_premarket_high_pct=None,
        distance_from_resistance_pct=None,
        spread_abs=0.01,
        spread_pct=0.1,
        dollar_volume=1_000_000,
    )
    base.update(overrides)
    return MomentumMetrics(**base)


def _candidate(**overrides) -> Candidate:
    now = datetime.utcnow()
    base = dict(symbol="ABCD", state=CandidateState.ARMED, discovered_at=now, last_updated_at=now)
    base.update(overrides)
    return Candidate(**base)


def _snapshot(price: float = 11.0) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="ABCD", timestamp=datetime.utcnow(), last_price=price,
        bid=price - 0.01, ask=price + 0.01, bid_size=100, ask_size=100,
        cumulative_volume=100_000, vwap=10.0, high_of_day=price,
        low_of_day=9.0, open_price=9.5,
    )


def test_fires_on_volume_acceleration_surge():
    strategy = VolumeIgnitionStrategy()
    candidate = _candidate(latest_metrics=_metrics(volume_accel_1m_3m=4.0))
    signal = strategy.on_snapshot(candidate, _snapshot())
    assert signal is not None
    assert signal.action == SignalAction.ENTER_LONG
    assert signal.suggested_stop is not None
    assert signal.suggested_stop < signal.reference_price
    assert signal.suggested_target is None


def test_fires_on_float_velocity_surge_alone():
    strategy = VolumeIgnitionStrategy()
    candidate = _candidate(latest_metrics=_metrics(volume_accel_1m_3m=1.0, float_velocity_5m=0.05))
    signal = strategy.on_snapshot(candidate, _snapshot())
    assert signal is not None


def test_no_signal_when_neither_volume_nor_float_igniting():
    strategy = VolumeIgnitionStrategy()
    candidate = _candidate(latest_metrics=_metrics(volume_accel_1m_3m=1.0, float_velocity_5m=0.01))
    assert strategy.on_snapshot(candidate, _snapshot()) is None


def test_no_signal_when_price_falling():
    strategy = VolumeIgnitionStrategy()
    candidate = _candidate(latest_metrics=_metrics(price_velocity_1m=-1.0))
    assert strategy.on_snapshot(candidate, _snapshot()) is None


def test_no_signal_when_below_vwap():
    strategy = VolumeIgnitionStrategy()
    candidate = _candidate(latest_metrics=_metrics(distance_from_vwap_pct=-1.0))
    assert strategy.on_snapshot(candidate, _snapshot()) is None


def test_no_signal_when_spread_too_wide():
    config = VolumeIgnitionConfig(max_spread_pct=1.0)
    strategy = VolumeIgnitionStrategy(config)
    candidate = _candidate(latest_metrics=_metrics(spread_pct=2.0))
    assert strategy.on_snapshot(candidate, _snapshot()) is None


def test_no_signal_when_not_armed():
    strategy = VolumeIgnitionStrategy()
    candidate = _candidate(state=CandidateState.WATCHING, latest_metrics=_metrics())
    assert strategy.on_snapshot(candidate, _snapshot()) is None


def test_no_signal_without_metrics():
    strategy = VolumeIgnitionStrategy()
    candidate = _candidate(latest_metrics=None)
    assert strategy.on_snapshot(candidate, _snapshot()) is None
