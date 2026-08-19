from datetime import datetime, timedelta

from webull_bot.enums import MomentumPhase
from webull_bot.models import MomentumMetrics
from webull_bot.scoring.rtms import RTMSConfig, compute_rtms, compute_rtms_components


def _build_metrics(**overrides) -> MomentumMetrics:
    base = dict(
        symbol="ABCD",
        timestamp=datetime(2026, 8, 17, 15, 0, 0),
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
    )
    base.update(overrides)
    return MomentumMetrics(**base)


def test_config_weights_sum_to_one():
    config = RTMSConfig.load()
    assert abs(sum(config.weights.values()) - 1.0) < 1e-9


def test_config_strategy_phase_policy_loaded_for_all_eight_strategies():
    config = RTMSConfig.load()
    expected = {
        "momentum_breakout", "refined_breakout", "opening_range_breakout",
        "breakout_pullback", "ignition_pullback", "vwap_reclaim",
        "volatility_contraction", "volume_ignition",
    }
    assert set(config.strategy_phase_policy.keys()) == expected
    # volume_ignition must never be allowed to trigger out of a pullback --
    # explicit spec requirement (see rtms_weights.yaml's own comment).
    assert config.strategy_phase_policy["volume_ignition"] == frozenset({MomentumPhase.IMPULSING})


def test_compute_rtms_none_metrics_returns_zero():
    config = RTMSConfig.load()
    score, components = compute_rtms(None, config, datetime.utcnow())
    assert score == 0.0
    assert components.momentum_15s_score is None


def test_compute_rtms_strong_metrics_scores_high():
    config = RTMSConfig.load()
    now = datetime(2026, 8, 17, 15, 0, 10)
    metrics = _build_metrics(
        return_15s=1.5, return_30s=3.0, return_60s=5.0,
        acceleration_15s=0.8, trend_efficiency_15s=0.9,
        recent_high_15s=10.10, recent_high_15s_time=now - timedelta(seconds=1),
        order_flow_imbalance_1m=0.5, order_flow_sample_count_1m=20,
        volume_accel_1m_3m=3.0,
    )
    score, components = compute_rtms(metrics, config, now, current_price=10.10)
    assert score > 80.0
    assert components.momentum_15s_score == 100.0


def test_compute_rtms_weak_metrics_scores_low():
    config = RTMSConfig.load()
    now = datetime(2026, 8, 17, 15, 0, 10)
    metrics = _build_metrics(
        return_15s=0.05, return_30s=0.1, return_60s=0.2,
        acceleration_15s=-0.5, trend_efficiency_15s=0.1,
        recent_high_15s=None, recent_high_15s_time=None,
        order_flow_imbalance_1m=None, order_flow_sample_count_1m=0,
        volume_accel_1m_3m=0.5,
    )
    score, components = compute_rtms(metrics, config, now, current_price=10.0)
    assert score < 20.0


def test_fresh_high_reclaim_score_none_without_recent_high_context():
    config = RTMSConfig.load()
    now = datetime(2026, 8, 17, 15, 0, 10)
    metrics = _build_metrics(recent_high_15s=None, recent_high_15s_time=None)
    components = compute_rtms_components(metrics, config, now, current_price=10.0)
    assert components.fresh_high_reclaim_score is None


def test_fresh_high_reclaim_score_zero_once_price_falls_too_far_below_high():
    config = RTMSConfig.load()
    now = datetime(2026, 8, 17, 15, 0, 10)
    metrics = _build_metrics(recent_high_15s=10.0, recent_high_15s_time=now - timedelta(seconds=1))
    components = compute_rtms_components(metrics, config, now, current_price=9.0)  # >10% below
    assert components.fresh_high_reclaim_score == 0.0


def test_order_flow_component_ignored_below_sample_floor():
    config = RTMSConfig.load()
    now = datetime(2026, 8, 17, 15, 0, 10)
    metrics = _build_metrics(order_flow_imbalance_1m=0.9, order_flow_sample_count_1m=1)
    components = compute_rtms_components(metrics, config, now, current_price=10.0)
    assert components.order_flow_trade_velocity_score is None


# -- regime_distance_score (rtms-v4, 2026-08-19, explicit user request) -----
# Heaviest-weighted RTMS component by a wide margin -- see rtms_weights.yaml's
# rtms-v4 changelog entry. Measures percentage points metrics.return_5m
# clears min_return_5m_pct by, NOT return_5m itself.

def test_regime_distance_score_is_the_heaviest_weight():
    config = RTMSConfig.load()
    assert config.weights["regime_distance_score"] == max(config.weights.values())
    # Wide margin, not just technically highest -- "dominates" per the
    # explicit ask, more than 3x the next-heaviest component.
    other_weights = [w for k, w in config.weights.items() if k != "regime_distance_score"]
    assert config.weights["regime_distance_score"] > 3 * max(other_weights)


def test_regime_distance_score_zero_right_at_the_floor():
    config = RTMSConfig.load()
    now = datetime(2026, 8, 17, 15, 0, 10)
    # min_return_5m_pct defaults to 4.00 -- exactly at the floor scores 0,
    # same "0 at/below minimum" contract as every other RTMS curve.
    metrics = _build_metrics(return_5m=4.00)
    components = compute_rtms_components(metrics, config, now, current_price=10.0)
    assert components.regime_distance_score == 0.0


def test_regime_distance_score_maxes_out_far_above_the_floor():
    config = RTMSConfig.load()
    now = datetime(2026, 8, 17, 15, 0, 10)
    # BIVI/BTCT-shaped: 17-22 points clear of the 4.00% floor -- both real
    # incidents this component exists to reward on ranking.
    metrics = _build_metrics(return_5m=21.72)
    components = compute_rtms_components(metrics, config, now, current_price=10.0)
    assert components.regime_distance_score == 100.0


def test_regime_distance_score_none_without_a_5m_return():
    config = RTMSConfig.load()
    now = datetime(2026, 8, 17, 15, 0, 10)
    metrics = _build_metrics(return_5m=None)
    components = compute_rtms_components(metrics, config, now, current_price=10.0)
    assert components.regime_distance_score is None


def test_regime_distance_score_dominates_the_final_rtms_score():
    """Two candidates identical on every OTHER component -- only distance
    above the regime floor differs. The one far past the floor must score
    dramatically higher, proving this component actually drives the score
    (not just carries the biggest number on paper)."""
    config = RTMSConfig.load()
    now = datetime(2026, 8, 17, 15, 0, 10)
    weak_other_components = dict(
        return_15s=0.1, return_30s=0.2, return_60s=0.3, acceleration_15s=-0.2,
        trend_efficiency_15s=0.2, recent_high_15s=None, recent_high_15s_time=None,
        order_flow_imbalance_1m=None, order_flow_sample_count_1m=0, volume_accel_1m_3m=0.5,
    )
    at_floor = _build_metrics(return_5m=4.00, **weak_other_components)
    far_past_floor = _build_metrics(return_5m=21.72, **weak_other_components)

    score_at_floor, _ = compute_rtms(at_floor, config, now, current_price=10.0)
    score_far_past, _ = compute_rtms(far_past_floor, config, now, current_price=10.0)
    assert score_far_past - score_at_floor > 35.0  # dominant, not marginal
