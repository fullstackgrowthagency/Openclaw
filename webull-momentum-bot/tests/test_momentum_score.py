from datetime import datetime

from webull_bot.models import FloatData, MomentumMetrics
from webull_bot.scoring.momentum_ignition_score import MISConfig, compute_score


def _metrics(**overrides) -> MomentumMetrics:
    base = dict(
        symbol="ABCD",
        timestamp=datetime.utcnow(),
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


def _float_data(free_float: float) -> FloatData:
    return FloatData(
        symbol="ABCD",
        free_float_shares=free_float,
        shares_outstanding=free_float * 1.2,
        market_cap=None,
        float_percent=None,
        effective_date=None,
        fetched_at=datetime.utcnow(),
    )


def test_weights_normalize_to_one():
    config = MISConfig.load()
    assert abs(sum(config.weights.values()) - 1.0) < 1e-9


def test_score_is_bounded_0_to_100():
    config = MISConfig.load()
    metrics = _metrics(relative_volume=10.0, volume_accel_1m_3m=5.0, price_acceleration=20.0)
    score = compute_score(metrics, _float_data(5_000_000), config)
    assert 0.0 <= score.score <= 100.0


def test_low_float_scores_higher_than_high_float_all_else_equal():
    config = MISConfig.load()
    metrics = _metrics()
    low_float_score = compute_score(metrics, _float_data(3_000_000), config).score
    high_float_score = compute_score(metrics, _float_data(19_000_000), config).score
    assert low_float_score > high_float_score


def test_strong_momentum_scores_higher_than_flat():
    config = MISConfig.load()
    flat = _metrics()
    strong = _metrics(
        relative_volume=8.0,
        volume_accel_1m_3m=3.0,
        price_acceleration=4.0,
        distance_from_resistance_pct=1.0,
        distance_from_hod_pct=1.0,
        distance_from_vwap_pct=3.0,
    )
    float_data = _float_data(5_000_000)
    flat_score = compute_score(flat, float_data, config).score
    strong_score = compute_score(strong, float_data, config).score
    assert strong_score > flat_score


def test_missing_float_data_scores_float_component_zero():
    config = MISConfig.load()
    metrics = _metrics()
    score = compute_score(metrics, None, config)
    assert score.components.float_score == 0.0


# -- v2 additions: float_turnover_score / short_term_relative_volume_score /
# dollar_volume_acceleration_score -- these consume metrics that
# metrics/rolling.py already computed (float_turnover, relative_volume_5m,
# dollar_volume_accel_1m_3m) but the v1 score formula never used. -------------

def test_high_float_turnover_scores_higher_than_low():
    config = MISConfig.load()
    float_data = _float_data(5_000_000)
    low = compute_score(_metrics(float_turnover=0.01), float_data, config)
    high = compute_score(_metrics(float_turnover=0.9), float_data, config)
    assert high.components.float_turnover_score > low.components.float_turnover_score
    assert high.score > low.score


def test_high_short_term_relative_volume_scores_higher_than_low():
    config = MISConfig.load()
    float_data = _float_data(5_000_000)
    low = compute_score(_metrics(relative_volume_5m=1.0), float_data, config)
    high = compute_score(_metrics(relative_volume_5m=10.0), float_data, config)
    assert high.components.short_term_relative_volume_score > low.components.short_term_relative_volume_score
    assert high.score > low.score


def test_high_dollar_volume_acceleration_scores_higher_than_low():
    config = MISConfig.load()
    float_data = _float_data(5_000_000)
    low = compute_score(_metrics(dollar_volume_accel_1m_3m=1.0), float_data, config)
    high = compute_score(_metrics(dollar_volume_accel_1m_3m=3.0), float_data, config)
    assert high.components.dollar_volume_acceleration_score > low.components.dollar_volume_acceleration_score
    assert high.score > low.score


def test_current_activity_outweighs_structural_factors_in_default_weights():
    # Regression for the explicit reweighting: a candidate seeing heavy
    # real-time activity right now (high RVOL/float turnover/dollar-volume
    # acceleration) should outrank one that only looks structurally
    # attractive (tighter spread, closer to breakout, better VWAP trend)
    # but isn't actually being traded heavily -- since the dashboard sorts
    # candidates by score descending, this is what determines who's "at the
    # top." See weights.yaml's v2 comment.
    config = MISConfig.load()
    float_data = _float_data(5_000_000)

    structurally_attractive_but_quiet = _metrics(
        distance_from_resistance_pct=1.5, distance_from_hod_pct=1.5, distance_from_vwap_pct=4.0,
        spread_pct=0.05,
    )
    actively_popular_right_now = _metrics(
        relative_volume=6.0, relative_volume_5m=8.0, float_turnover=0.6,
        volume_accel_1m_3m=2.5, dollar_volume_accel_1m_3m=2.5,
    )

    quiet_score = compute_score(structurally_attractive_but_quiet, float_data, config).score
    popular_score = compute_score(actively_popular_right_now, float_data, config).score
    assert popular_score > quiet_score
