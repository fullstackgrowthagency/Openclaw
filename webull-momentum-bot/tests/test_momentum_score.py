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


# -- room_to_target_score / entry-selectivity rework (2026-08-13) -----------

def test_room_to_target_score_is_none_without_price_context():
    # Without current_price/target_pct, compute_score can't evaluate target
    # clearance at all -- must stay None (unavailable), never a fabricated
    # 0, and must not be included in the weighted average (see
    # compute_score's renormalization).
    config = MISConfig.load()
    score = compute_score(_metrics(), _float_data(5_000_000), config)
    assert score.components.room_to_target_score is None


def test_room_to_target_score_is_100_with_no_known_resistance():
    config = MISConfig.load()
    score = compute_score(
        _metrics(), _float_data(5_000_000), config,
        current_price=10.0, target_pct=0.10, static_resistance_levels=[],
    )
    assert score.components.room_to_target_score == 100.0


def test_room_to_target_score_is_0_when_resistance_sits_at_or_before_target():
    config = MISConfig.load()
    # target = 10 * 1.10 = 11.0; a resistance level at 10.5 sits before it.
    score = compute_score(
        _metrics(), _float_data(5_000_000), config,
        current_price=10.0, target_pct=0.10, static_resistance_levels=[10.5],
    )
    assert score.components.room_to_target_score == 0.0


def test_open_room_to_target_scores_higher_than_tight_room():
    config = MISConfig.load()
    metrics = _metrics()
    float_data = _float_data(5_000_000)
    tight_room = compute_score(
        metrics, float_data, config, current_price=10.0, target_pct=0.10, static_resistance_levels=[11.05],
    )
    wide_open = compute_score(
        metrics, float_data, config, current_price=10.0, target_pct=0.10, static_resistance_levels=[],
    )
    assert wide_open.components.room_to_target_score > tight_room.components.room_to_target_score
    assert wide_open.score > tight_room.score


def test_compute_score_renormalizes_missing_component_instead_of_treating_it_as_zero():
    # A candidate scored WITHOUT price context (room_to_target_score=None)
    # must not be silently penalized as though that component scored 0 --
    # it's excluded from the average entirely and the remaining weights
    # renormalized over the active total instead.
    config = MISConfig.load()
    metrics = _metrics()
    float_data = _float_data(5_000_000)
    without_context = compute_score(metrics, float_data, config)

    # What the score WOULD be under the bug this renormalization avoids:
    # treating the missing component as a 0 contribution while still
    # dividing by the full weight total (1.0) rather than the active one.
    components = without_context.components
    naive_if_treated_as_zero = sum(
        getattr(components, key) * weight for key, weight in config.weights.items()
        if getattr(components, key) is not None
    )
    assert without_context.score > naive_if_treated_as_zero


# -- TICK-derived order flow (2026-08-14) -----------------------------------

def test_order_flow_score_is_none_without_enough_classified_samples():
    config = MISConfig.load()
    # Extreme imbalance, but only 3 classified prints -- below weights.yaml's
    # min_order_flow_sample_count (8 by default) -- must stay None/excluded,
    # same contract as room_to_target_score without price context.
    metrics = _metrics(order_flow_imbalance_1m=0.9, order_flow_sample_count_1m=3)
    score = compute_score(metrics, _float_data(5_000_000), config)
    assert score.components.order_flow_score is None


def test_order_flow_score_scales_imbalance_to_0_100_once_sample_floor_is_met():
    config = MISConfig.load()
    balanced = compute_score(
        _metrics(order_flow_imbalance_1m=0.0, order_flow_sample_count_1m=20), _float_data(5_000_000), config,
    )
    all_buy = compute_score(
        _metrics(order_flow_imbalance_1m=1.0, order_flow_sample_count_1m=20), _float_data(5_000_000), config,
    )
    all_sell = compute_score(
        _metrics(order_flow_imbalance_1m=-1.0, order_flow_sample_count_1m=20), _float_data(5_000_000), config,
    )
    assert balanced.components.order_flow_score == 50.0
    assert all_buy.components.order_flow_score == 100.0
    assert all_sell.components.order_flow_score == 0.0
    assert all_buy.score > balanced.score > all_sell.score


def test_order_flow_score_is_none_when_metrics_never_saw_any_ticks():
    # The pre-TICK/default case (compute_metrics never given `ticks`) --
    # order_flow_imbalance_1m is None on the metrics object itself, not
    # just under-sampled.
    config = MISConfig.load()
    score = compute_score(_metrics(), _float_data(5_000_000), config)
    assert score.components.order_flow_score is None


# -- price_momentum_score (2026-08-19) -- raw upward price velocity, ------
# distinct from price_acceleration_score which only measures whether the
# move is speeding up, not whether/how much price is actually moving up.

def test_price_momentum_score_is_zero_when_flat():
    config = MISConfig.load()
    score = compute_score(_metrics(price_velocity_1m=0.0, price_velocity_5m=0.0), _float_data(5_000_000), config)
    assert score.components.price_momentum_score == 0.0


def test_price_momentum_score_is_zero_when_red():
    config = MISConfig.load()
    score = compute_score(_metrics(price_velocity_1m=-2.0, price_velocity_5m=-3.0), _float_data(5_000_000), config)
    assert score.components.price_momentum_score == 0.0


def test_price_momentum_score_scales_with_strength_not_just_pass_fail():
    # The three-point curve should give "barely positive" and "screaming
    # higher" meaningfully different scores, not both maxing out past a
    # single threshold -- confirms the scale3 curve is actually wired in,
    # not a plain two-point _scale.
    config = MISConfig.load()
    float_data = _float_data(5_000_000)
    barely_positive = compute_score(
        _metrics(price_velocity_1m=0.4, price_velocity_5m=1.2), float_data, config,
    ).components.price_momentum_score
    strong = compute_score(
        _metrics(price_velocity_1m=1.5, price_velocity_5m=4.0), float_data, config,
    ).components.price_momentum_score
    exceptional = compute_score(
        _metrics(price_velocity_1m=3.0, price_velocity_5m=7.0), float_data, config,
    ).components.price_momentum_score
    assert 0.0 < barely_positive < strong < exceptional == 100.0


def test_strong_upward_price_movement_outranks_an_otherwise_identical_flat_candidate():
    # This is the whole point: a candidate genuinely running up should now
    # outrank an otherwise-identical one that's flat, purely on the new
    # component -- previously nothing in MIS scored raw upward price
    # movement at all (only price_acceleration_score, which measures
    # whether the move is speeding up, not the move itself).
    config = MISConfig.load()
    float_data = _float_data(5_000_000)
    flat = compute_score(_metrics(price_velocity_1m=0.0, price_velocity_5m=0.0), float_data, config)
    running_up = compute_score(_metrics(price_velocity_1m=2.0, price_velocity_5m=5.0), float_data, config)
    assert running_up.components.price_momentum_score > flat.components.price_momentum_score
    assert running_up.score > flat.score


def test_price_momentum_score_blends_both_windows():
    # Strong on one window but flat on the other should land in between --
    # confirms this is a genuine average of the two, not just reading one.
    config = MISConfig.load()
    float_data = _float_data(5_000_000)
    both_strong = compute_score(_metrics(price_velocity_1m=2.0, price_velocity_5m=5.0), float_data, config)
    only_1m_strong = compute_score(_metrics(price_velocity_1m=2.0, price_velocity_5m=0.0), float_data, config)
    only_5m_strong = compute_score(_metrics(price_velocity_1m=0.0, price_velocity_5m=5.0), float_data, config)
    assert only_1m_strong.components.price_momentum_score < both_strong.components.price_momentum_score
    assert only_5m_strong.components.price_momentum_score < both_strong.components.price_momentum_score
    assert only_1m_strong.components.price_momentum_score > 0.0
    assert only_5m_strong.components.price_momentum_score > 0.0
