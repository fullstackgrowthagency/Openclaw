from datetime import datetime

from webull_bot.enums import CandidateState, SignalAction
from webull_bot.models import Candidate, MarketSnapshot, MomentumMetrics
from webull_bot.strategy.breakout_pullback import BreakoutPullbackConfig, BreakoutPullbackStrategy, _Phase


def _metrics(**overrides) -> MomentumMetrics:
    base = dict(
        symbol="ABCD", timestamp=datetime.utcnow(),
        float_turnover=0.1, float_velocity_1m=0.01, float_velocity_3m=0.02, float_velocity_5m=0.01,
        relative_volume=1.0, relative_volume_1m=1.0, relative_volume_5m=1.0,
        volume_accel_1m_3m=1.5,
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
    base = dict(symbol="ABCD", state=CandidateState.ARMED, discovered_at=now, last_updated_at=now, resistance_level=10.0)
    base.update(overrides)
    return Candidate(**base)


def _snapshot(price: float) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="ABCD", timestamp=datetime.utcnow(), last_price=price,
        bid=price - 0.01, ask=price + 0.01, bid_size=100, ask_size=100,
        cumulative_volume=100_000, vwap=10.0, high_of_day=price, low_of_day=9.0, open_price=9.5,
    )


def test_full_breakout_pullback_reclaim_sequence_fires():
    strategy = BreakoutPullbackStrategy()
    candidate = _candidate()
    candidate.latest_metrics = _metrics()

    # Breakout above resistance.
    assert strategy.on_snapshot(candidate, _snapshot(10.5)) is None
    assert strategy._phase["ABCD"] == _Phase.PULLBACK_FORMING

    # Pullback, two bars (default min_pullback_bars=2), then a third bar
    # turning back up off the low -- arms READY_TO_ENTER.
    assert strategy.on_snapshot(candidate, _snapshot(10.3)) is None  # bar 1: dips
    assert strategy.on_snapshot(candidate, _snapshot(10.25)) is None  # bar 2: dips further
    assert strategy.on_snapshot(candidate, _snapshot(10.3)) is None  # bar 3: turns up
    assert strategy._phase["ABCD"] == _Phase.READY_TO_ENTER

    # Entry: price clears the reclaim buffer above the pullback low (10.25).
    signal = strategy.on_snapshot(candidate, _snapshot(10.4))
    assert signal is not None
    assert signal.action == SignalAction.ENTER_LONG
    assert signal.suggested_stop < signal.reference_price
    # Phase resets so a fresh breakout must be detected before firing again.
    assert strategy._phase["ABCD"] == _Phase.AWAITING_BREAKOUT


def test_reversal_before_min_pullback_bars_does_not_arm_entry():
    # Real bug fixed 2026-08-12: min_pullback_bars was declared
    # ("require at least this many snapshots of pulling back") but never
    # actually read -- a pullback lasting fewer bars than configured used
    # to arm READY_TO_ENTER just as readily as a genuine one. Pinned above
    # the natural 2-bar minimum (any reversal needs at least one dip bar
    # plus one reversal bar) specifically so this test distinguishes the
    # fix from that baseline, not just from an untested default.
    strategy = BreakoutPullbackStrategy(BreakoutPullbackConfig(min_pullback_bars=3))
    candidate = _candidate()
    candidate.latest_metrics = _metrics()

    strategy.on_snapshot(candidate, _snapshot(10.5))  # breakout
    strategy.on_snapshot(candidate, _snapshot(10.3))  # bar 1: dips to 10.3

    # bar 2: turns back up (10.35 > pullback_low 10.3) -- only 2 bars spent
    # in the pullback, below the configured minimum of 3.
    assert strategy.on_snapshot(candidate, _snapshot(10.35)) is None
    assert strategy._phase["ABCD"] == _Phase.PULLBACK_FORMING  # NOT armed yet

    # bar 3: still above the pullback low -- now the minimum is met.
    assert strategy.on_snapshot(candidate, _snapshot(10.4)) is None
    assert strategy._phase["ABCD"] == _Phase.READY_TO_ENTER


def test_deep_pullback_invalidates_and_resets():
    strategy = BreakoutPullbackStrategy(BreakoutPullbackConfig(max_pullback_retrace_pct=20.0))
    candidate = _candidate()
    candidate.latest_metrics = _metrics()
    strategy.on_snapshot(candidate, _snapshot(10.5))  # breakout, resistance=10.0

    # Retraces more than 20% of the (10.5 - 10.0) breakout move -- invalidates.
    assert strategy.on_snapshot(candidate, _snapshot(10.05)) is None
    assert strategy._phase["ABCD"] == _Phase.AWAITING_BREAKOUT
    assert candidate.breakout_price is None
    assert candidate.pullback_low is None
    assert "ABCD" not in strategy._pullback_bars


def test_no_signal_when_not_armed():
    strategy = BreakoutPullbackStrategy()
    candidate = _candidate(state=CandidateState.WATCHING, latest_metrics=_metrics())
    assert strategy.on_snapshot(candidate, _snapshot(10.5)) is None


def test_no_signal_without_resistance_level():
    strategy = BreakoutPullbackStrategy()
    candidate = _candidate(resistance_level=None, latest_metrics=_metrics())
    assert strategy.on_snapshot(candidate, _snapshot(10.5)) is None


def test_target_follows_injected_reward_risk_ratio():
    strategy = BreakoutPullbackStrategy(reward_risk_ratio_fn=lambda: 3.0)
    candidate = _candidate()
    candidate.latest_metrics = _metrics()

    strategy.on_snapshot(candidate, _snapshot(10.5))
    strategy.on_snapshot(candidate, _snapshot(10.3))
    strategy.on_snapshot(candidate, _snapshot(10.25))
    strategy.on_snapshot(candidate, _snapshot(10.3))
    signal = strategy.on_snapshot(candidate, _snapshot(10.4))

    assert signal is not None
    risk_per_share = signal.reference_price - signal.suggested_stop
    assert signal.suggested_target == signal.reference_price + risk_per_share * 3.0
