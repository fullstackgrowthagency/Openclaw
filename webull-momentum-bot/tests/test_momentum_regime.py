from datetime import datetime

from webull_bot.enums import CandidateState, SignalAction
from webull_bot.models import Candidate, MarketSnapshot, MomentumMetrics
from webull_bot.strategy.momentum_regime import MomentumRegimeConfig, MomentumRegimeStrategy


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
        volume_accel_1m_3m=1.0,
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
        return_5m=5.0,
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


def test_fires_when_return_5m_clears_regime_bar():
    strategy = MomentumRegimeStrategy()
    candidate = _candidate(latest_metrics=_metrics(return_5m=4.5))
    signal = strategy.on_snapshot(candidate, _snapshot())
    assert signal is not None
    assert signal.action == SignalAction.ENTER_LONG
    assert signal.strategy_name == "momentum_regime"
    assert signal.metadata["return_5m"] == 4.5
    assert signal.suggested_stop is not None
    assert signal.suggested_stop < signal.reference_price
    risk_per_share = signal.reference_price - signal.suggested_stop
    assert signal.suggested_target == signal.reference_price + risk_per_share * 2.0


def test_target_follows_injected_reward_risk_ratio():
    strategy = MomentumRegimeStrategy(reward_risk_ratio_fn=lambda: 3.0)
    candidate = _candidate(latest_metrics=_metrics(return_5m=4.5))
    signal = strategy.on_snapshot(candidate, _snapshot())
    assert signal is not None
    risk_per_share = signal.reference_price - signal.suggested_stop
    assert signal.suggested_target == signal.reference_price + risk_per_share * 3.0


def test_stop_follows_injected_stop_loss_pct():
    strategy = MomentumRegimeStrategy(stop_loss_pct_fn=lambda: 5.0)
    candidate = _candidate(latest_metrics=_metrics(return_5m=4.5))
    signal = strategy.on_snapshot(candidate, _snapshot(price=100.0))
    assert signal is not None
    assert signal.suggested_stop == 95.0


def test_no_signal_when_not_armed():
    strategy = MomentumRegimeStrategy()
    candidate = _candidate(state=CandidateState.WATCHING, latest_metrics=_metrics(return_5m=4.5))
    assert strategy.on_snapshot(candidate, _snapshot()) is None


def test_no_signal_without_metrics():
    strategy = MomentumRegimeStrategy()
    candidate = _candidate(latest_metrics=None)
    assert strategy.on_snapshot(candidate, _snapshot()) is None


def test_no_signal_when_return_5m_not_yet_available():
    strategy = MomentumRegimeStrategy()
    candidate = _candidate(latest_metrics=_metrics(return_5m=None))
    assert strategy.on_snapshot(candidate, _snapshot()) is None


def test_no_signal_when_below_regime_threshold():
    strategy = MomentumRegimeStrategy()
    candidate = _candidate(latest_metrics=_metrics(return_5m=3.9))
    assert strategy.on_snapshot(candidate, _snapshot()) is None


def test_no_signal_when_spread_too_wide():
    config = MomentumRegimeConfig(max_spread_pct=1.0)
    strategy = MomentumRegimeStrategy(config)
    candidate = _candidate(latest_metrics=_metrics(return_5m=4.5, spread_pct=2.0))
    assert strategy.on_snapshot(candidate, _snapshot()) is None


def test_custom_threshold_override_respected():
    config = MomentumRegimeConfig(min_return_5m_pct=6.0)
    strategy = MomentumRegimeStrategy(config)
    candidate = _candidate(latest_metrics=_metrics(return_5m=4.5))
    assert strategy.on_snapshot(candidate, _snapshot()) is None

    candidate = _candidate(latest_metrics=_metrics(return_5m=6.5))
    assert strategy.on_snapshot(candidate, _snapshot()) is not None
