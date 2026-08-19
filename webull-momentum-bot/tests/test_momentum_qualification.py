"""
Named-scenario tests for the Real-Time Momentum Qualification Layer
(scanner/momentum_qualification.py). Scenario letters match the spec this
layer was built from:
  A) slow grinder -- never clears the regime gate, stays rejected/ARMED
  B) strong breakout -- IMPULSING
  C) stale breakout -- quality gate holds it back from IMPULSING
  D) healthy pullback -- PULLING_BACK, tracked not rejected/entered
  E) pullback -> reacceleration -- entry-eligible again
  F) pullback invalidated (broken structure/excess retracement) -- revert
(Scenario G, "stale queued confirmation blocked, needs fresh qualification",
is a TradingLoop-level integration behavior -- see
tests/test_trading_loop.py's test_submit_ranked_entries_final_recheck_*.)
"""
from datetime import datetime, timedelta

from webull_bot.enums import MomentumPhase
from webull_bot.models import MarketSnapshot, MomentumMetrics, SignalAction, Signal
from webull_bot.scanner.momentum_qualification import MomentumQualificationEngine
from webull_bot.scoring.rtms import RTMSConfig
from webull_bot.state_machine import new_candidate

_NOW = datetime(2026, 8, 17, 15, 0, 0)


def _metrics(**overrides) -> MomentumMetrics:
    base = dict(
        symbol="TEST",
        timestamp=_NOW,
        float_turnover=0.1,
        float_velocity_1m=0.01,
        float_velocity_3m=0.02,
        float_velocity_5m=0.03,
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
        price_velocity_1m=0.0,
        price_velocity_3m=0.0,
        price_velocity_5m=0.0,
        price_velocity_15m=0.0,
        price_acceleration=0.0,
        vwap=10.0,
        distance_from_vwap_pct=0.0,
        distance_from_hod_pct=0.0,
        distance_from_premarket_high_pct=None,
        distance_from_resistance_pct=None,
        spread_abs=0.01,
        spread_pct=0.1,
        dollar_volume=1_000_000,
        return_5s=0.0,
        return_15s=0.0,
        return_30s=0.0,
        return_60s=0.0,
        return_5m=0.0,
        velocity_5s=0.0,
        acceleration_60s=0.0,
        trend_efficiency_15s=0.0,
        recent_high_15s=None,
        recent_high_15s_time=None,
        order_flow_imbalance_1m=None,
        order_flow_sample_count_1m=0,
    )
    base.update(overrides)
    return MomentumMetrics(**base)


def _snapshot(price: float, t: datetime = _NOW) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="TEST", timestamp=t, last_price=price, bid=price - 0.01, ask=price + 0.01,
        bid_size=100, ask_size=100, cumulative_volume=100_000, vwap=10.0, high_of_day=price,
        low_of_day=price - 1.0, open_price=price - 0.5,
    )


def _candidate():
    return new_candidate("TEST", now=_NOW)


def _signal(strategy_name="momentum_breakout", reference_price=10.0):
    return Signal(
        symbol="TEST", action=SignalAction.ENTER_LONG, generated_at=_NOW, strategy_name=strategy_name,
        strategy_version="v1", reference_price=reference_price, suggested_stop=reference_price * 0.95,
        suggested_target=reference_price * 1.10, score_at_signal=80.0, metadata={},
    )


def _engine():
    return MomentumQualificationEngine(RTMSConfig.load())


# -- A: slow grinder rejected -------------------------------------------------

def test_scenario_a_slow_grinder_never_qualifies():
    engine = _engine()
    candidate = _candidate()
    candidate.latest_metrics = _metrics(
        return_5m=2.0, return_60s=0.3, return_30s=0.15, return_15s=0.05, return_5s=0.01,
        trend_efficiency_15s=0.4,
    )
    signal = _signal()
    engine.on_snapshot(candidate, signal, _snapshot(10.0), _NOW)
    decision = engine.evaluate_trigger(candidate, signal, _snapshot(10.0), _NOW)
    assert decision.outcome == "stay_armed"
    assert candidate.momentum.phase in (MomentumPhase.NONE, MomentumPhase.IGNITING)
    assert candidate.momentum.momentum_qualified is False


# -- B: strong breakout -> IMPULSING -----------------------------------------

def test_scenario_b_strong_breakout_reaches_impulsing():
    engine = _engine()
    candidate = _candidate()
    candidate.latest_metrics = _metrics(
        return_5m=6.0, return_60s=2.0, return_30s=1.5, return_15s=0.8, return_5s=0.1,
        trend_efficiency_15s=0.9, recent_high_15s=10.10, recent_high_15s_time=_NOW - timedelta(seconds=1),
        acceleration_60s=0.5,
    )
    signal = _signal()
    engine.on_snapshot(candidate, signal, _snapshot(10.10), _NOW)
    assert candidate.momentum.phase == MomentumPhase.IMPULSING
    assert candidate.momentum.impulse_high == 10.10


# -- C: stale breakout -- fails the freshness/proximity quality gate --------

def test_scenario_c_stale_breakout_does_not_reach_impulsing():
    engine = _engine()
    candidate = _candidate()
    # Same strong returns as B, but the "recent high" was made 20s ago --
    # well past impulsing_high_freshness_seconds (10.0s, rtms-v2) -- so
    # quality fails.
    candidate.latest_metrics = _metrics(
        return_5m=6.0, return_60s=2.0, return_30s=1.5, return_15s=0.8, return_5s=0.1,
        trend_efficiency_15s=0.9, recent_high_15s=10.10, recent_high_15s_time=_NOW - timedelta(seconds=20),
        acceleration_60s=0.5,
    )
    signal = _signal()
    engine.on_snapshot(candidate, signal, _snapshot(10.10), _NOW)
    assert candidate.momentum.phase != MomentumPhase.IMPULSING


# -- D: healthy pullback -- tracked, not rejected or entered -----------------

def test_scenario_d_healthy_pullback_is_tracked_not_rejected():
    engine = _engine()
    candidate = _candidate()
    candidate.resistance_level = 9.80  # below the pullback price -> structure intact
    signal = _signal()

    # tick0: establish IMPULSING at 10.10.
    candidate.latest_metrics = _metrics(
        return_5m=6.0, return_60s=2.0, return_30s=1.5, return_15s=0.8, return_5s=0.1,
        trend_efficiency_15s=0.9, recent_high_15s=10.10, recent_high_15s_time=_NOW - timedelta(seconds=1),
        acceleration_60s=0.5,
    )
    engine.on_snapshot(candidate, signal, _snapshot(10.10), _NOW)
    assert candidate.momentum.phase == MomentumPhase.IMPULSING

    # tick1: a small, structurally-intact pullback (well within both the
    # 50% retracement ratio and 1.5% absolute tolerance).
    t1 = _NOW + timedelta(seconds=5)
    candidate.latest_metrics = _metrics(return_5m=6.0, return_5s=-0.05)
    engine.on_snapshot(candidate, signal, _snapshot(10.00), t1)
    assert candidate.momentum.phase == MomentumPhase.PULLING_BACK
    assert candidate.momentum.structure_intact is True

    decision = engine.evaluate_trigger(candidate, signal, _snapshot(10.00), t1)
    assert decision.outcome == "track_pullback"


# -- E: pullback -> reacceleration -- entry-eligible again -------------------

def test_scenario_e_pullback_then_reacceleration_is_entry_eligible():
    engine = _engine()
    candidate = _candidate()
    candidate.resistance_level = 9.80
    signal = _signal()

    # tick0: IMPULSING at 10.10.
    candidate.latest_metrics = _metrics(
        return_5m=6.0, return_60s=2.0, return_30s=1.5, return_15s=0.8, return_5s=0.1,
        trend_efficiency_15s=0.9, recent_high_15s=10.10, recent_high_15s_time=_NOW - timedelta(seconds=1),
        acceleration_60s=0.5, velocity_5s=0.05,
    )
    engine.on_snapshot(candidate, signal, _snapshot(10.10), _NOW)
    assert candidate.momentum.phase == MomentumPhase.IMPULSING

    # tick1: pullback to 10.00, weak/negative short-term velocity.
    t1 = _NOW + timedelta(seconds=5)
    candidate.latest_metrics = _metrics(return_5m=6.0, return_5s=-0.05, velocity_5s=-0.02)
    engine.on_snapshot(candidate, signal, _snapshot(10.00), t1)
    assert candidate.momentum.phase == MomentumPhase.PULLING_BACK

    # tick2: price reclaims above tick1's micro-high (10.00) with rising
    # 5s velocity and a stronger RTMS reading than tick1 -- the two
    # "vs. immediately preceding tick" conditions REACCELERATING needs.
    t2 = t1 + timedelta(seconds=2)
    candidate.latest_metrics = _metrics(
        return_5m=6.2, return_60s=1.8, return_30s=1.2, return_15s=0.6, return_5s=0.08,
        trend_efficiency_15s=0.8, recent_high_15s=10.10, recent_high_15s_time=t2 - timedelta(seconds=1),
        acceleration_60s=0.4, velocity_5s=0.06,
    )
    engine.on_snapshot(candidate, signal, _snapshot(10.05), t2)
    assert candidate.momentum.phase == MomentumPhase.REACCELERATING

    decision = engine.evaluate_trigger(candidate, signal, _snapshot(10.05), t2)
    assert decision.rtms is not None
    # REACCELERATING is entry-eligible (subject to its own RTMS floor) --
    # never silently downgraded back to track_pullback/stay_armed just for
    # having pulled back earlier in the same impulse.
    assert decision.outcome in ("start_confirmation", "stay_armed")
    if decision.outcome == "stay_armed":
        assert "RTMS" in decision.reason  # only reason it may still be blocked


# -- F: pullback invalidated -- broken structure or excess retracement -------

def test_scenario_f_pullback_invalidated_by_broken_structure_reverts_to_none():
    engine = _engine()
    candidate = _candidate()
    candidate.resistance_level = 9.95  # ABOVE the drop below -- structure breaks
    signal = _signal()

    candidate.latest_metrics = _metrics(
        return_5m=6.0, return_60s=2.0, return_30s=1.5, return_15s=0.8, return_5s=0.1,
        trend_efficiency_15s=0.9, recent_high_15s=10.10, recent_high_15s_time=_NOW - timedelta(seconds=1),
        acceleration_60s=0.5,
    )
    engine.on_snapshot(candidate, signal, _snapshot(10.10), _NOW)
    assert candidate.momentum.phase == MomentumPhase.IMPULSING

    # A hard drop through resistance -- structure broken regardless of
    # retracement size.
    t1 = _NOW + timedelta(seconds=5)
    candidate.latest_metrics = _metrics(return_5m=6.0)
    engine.on_snapshot(candidate, signal, _snapshot(9.50), t1)
    assert candidate.momentum.phase == MomentumPhase.NONE
    assert candidate.momentum.impulse_high is None  # full reset, not just a phase flip

    decision = engine.evaluate_trigger(candidate, signal, _snapshot(9.50), t1)
    assert decision.outcome == "stay_armed"


def test_scenario_f_variant_excess_retracement_reverts_even_with_structure_intact():
    engine = _engine()
    candidate = _candidate()
    candidate.resistance_level = 9.00  # far below -- structure alone stays intact
    signal = _signal()

    candidate.latest_metrics = _metrics(
        return_5m=6.0, return_60s=2.0, return_30s=1.5, return_15s=0.8, return_5s=0.1,
        trend_efficiency_15s=0.9, recent_high_15s=10.10, recent_high_15s_time=_NOW - timedelta(seconds=1),
        acceleration_60s=0.5,
    )
    engine.on_snapshot(candidate, signal, _snapshot(10.10), _NOW)
    assert candidate.momentum.phase == MomentumPhase.IMPULSING

    # A retracement of more than half the impulse AND more than 1.5%
    # absolute -- excess even though the (very distant) resistance level
    # never actually breaks.
    t1 = _NOW + timedelta(seconds=5)
    candidate.latest_metrics = _metrics(return_5m=6.0)
    engine.on_snapshot(candidate, signal, _snapshot(9.90), t1)
    assert candidate.momentum.phase == MomentumPhase.NONE
