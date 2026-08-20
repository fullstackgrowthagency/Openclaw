"""
Momentum Ignition Score (MIS): a configurable 0-100 ranking that says how
strongly a candidate matches the "low float + float velocity + RVOL +
volume acceleration + price acceleration + breakout proximity + liquidity"
pattern described in the project outline.

Crucially: a high MIS does NOT place a trade. It only feeds the state
machine's WATCHING -> HEATING_UP -> ARMED transitions (see scanner/
candidate_watcher.py). Actual entries additionally require a Strategy's
real-time confirmation logic and risk engine approval.

All weights and thresholds live in weights.yaml so they can be tuned from
backtest/paper-trading results without code changes.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

import yaml

from ..metrics.calculations import scale3
from ..metrics.volume_profile import evaluate_target_clearance
from ..models import FloatData, MomentumMetrics, MomentumScore, MomentumScoreComponents

_DEFAULT_WEIGHTS_PATH = Path(__file__).parent / "weights.yaml"


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def _scale(value: float, low: float, high: float) -> float:
    """Linearly map value in [low, high] to [0, 100], clamped at the ends."""
    if high == low:
        return 0.0
    return _clamp((value - low) / (high - low) * 100.0)


@dataclass(frozen=True)
class MISConfig:
    version: str
    weights: dict[str, float]
    thresholds: dict[str, float]

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "MISConfig":
        path = path or _DEFAULT_WEIGHTS_PATH
        raw = yaml.safe_load(path.read_text())
        weights = dict(raw["weights"])
        total = sum(weights.values()) or 1.0
        normalized = {k: v / total for k, v in weights.items()}
        return cls(version=raw["version"], weights=normalized, thresholds=dict(raw["thresholds"]))


def compute_components(
    metrics: MomentumMetrics,
    float_data: Optional[FloatData],
    config: MISConfig,
    *,
    current_price: Optional[float] = None,
    target_pct: Optional[float] = None,
    static_resistance_levels: Optional[Sequence[float]] = None,
) -> MomentumScoreComponents:
    th = config.thresholds

    # Lower float => higher score. Below the "preferred" threshold maxes out.
    if float_data is None or not float_data.free_float_shares:
        float_score = 0.0
    else:
        ff = float_data.free_float_shares
        if ff <= th["preferred_free_float_shares"]:
            float_score = 100.0
        elif ff >= th["max_free_float_shares"]:
            float_score = 0.0
        else:
            float_score = _scale(
                th["max_free_float_shares"] - ff,
                0,
                th["max_free_float_shares"] - th["preferred_free_float_shares"],
            )

    float_velocity_score = _scale(metrics.float_velocity_5m, 0.0, th["min_float_velocity_5m_for_armed"] * 2)
    relative_volume_score = _scale(metrics.relative_volume, 1.0, th["min_relative_volume_for_armed"] * 1.5)
    volume_acceleration_score = _scale(metrics.volume_accel_1m_3m, 1.0, 3.0)
    price_acceleration_score = _scale(metrics.price_acceleration, 0.0, 5.0)

    # -- v2 additions: already-computed-but-previously-unused metrics -------
    # today's cumulative float turnover -- distinct from float_velocity_5m
    # (a 5-minute rate): this is "how much of the float has changed hands
    # so far today," a strong signal a name is already a crowd favorite
    # rather than just starting to get hot.
    float_turnover_score = _scale(metrics.float_turnover, 0.0, th["min_float_turnover_for_notable"] * 2)
    # Windowed (5m) RVOL, more responsive to a fresh surge than the
    # whole-session relative_volume above -- reuses the same
    # min_relative_volume_for_armed bar since it's the same underlying
    # "notable RVOL" concept, just measured over a shorter, fresher window.
    short_term_relative_volume_score = _scale(metrics.relative_volume_5m, 1.0, th["min_relative_volume_for_armed"] * 1.5)
    # Dollar-volume analog of volume_acceleration_score -- genuinely
    # distinct, not a rescaled duplicate, since dollar_volume_accel_1m_3m
    # also reflects price movement between windows (see MomentumMetrics'
    # docstring for dollar_volume_accel_1m_3m).
    dollar_volume_acceleration_score = _scale(metrics.dollar_volume_accel_1m_3m, 1.0, 3.0)

    # Breakout proximity: closer to (or through) resistance/HOD scores higher.
    proximity_inputs = [
        d for d in (metrics.distance_from_resistance_pct, metrics.distance_from_hod_pct) if d is not None
    ]
    if proximity_inputs:
        closest = max(proximity_inputs)  # least-negative / most-positive = closest to or past the level
        breakout_proximity_score = _scale(closest, -10.0, 2.0)
    else:
        breakout_proximity_score = 0.0

    # Liquidity: tight spread + healthy dollar volume.
    spread_score = 100.0 - _scale(metrics.spread_pct, 0.0, th["max_spread_pct"])
    dollar_volume_score = _scale(metrics.dollar_volume, th["min_dollar_volume"] * 0.25, th["min_dollar_volume"] * 4)
    liquidity_score = (spread_score + dollar_volume_score) / 2

    # v2.2 addition: room to the fixed +stop*R target before a known static
    # resistance level gets in the way (metrics/volume_profile.py's
    # evaluate_target_clearance) -- see MomentumScoreComponents.room_to_target_score's
    # docstring for why this stays None (excluded from the weighted average,
    # not scored as 0) rather than defaulting to a real number when the
    # caller doesn't have price/resistance context to give it.
    room_to_target_score = None
    if current_price is not None and target_pct is not None:
        target_price = current_price * (1 + target_pct)
        room_to_target_score = evaluate_target_clearance(
            current_price, target_price, static_resistance_levels or [],
        ).room_to_target_score

    # v2.3 addition (2026-08-14, TICK-derived order flow -- see
    # docs/ARCHITECTURE.md): buy-vs-sell aggressor volume, the one signal
    # SNAPSHOT/QUOTE can't provide. Read straight off `metrics` (unlike
    # room_to_target_score above, this doesn't need extra current_price/
    # target_pct context -- order_flow_imbalance_1m/order_flow_sample_count_1m
    # are already fully computed by metrics/rolling.py's compute_metrics).
    # None (excluded from the weighted average, not scored as 0 -- same
    # contract as room_to_target_score) until at least
    # min_order_flow_sample_count real classified prints have accumulated,
    # so a thin/just-subscribed symbol isn't scored on a handful of noisy
    # prints.
    order_flow_score = None
    if (
        metrics.order_flow_imbalance_1m is not None
        and metrics.order_flow_sample_count_1m >= th["min_order_flow_sample_count"]
    ):
        order_flow_score = _scale(metrics.order_flow_imbalance_1m, -1.0, 1.0)

    # v2.4 addition (2026-08-19): raw upward price velocity, not just
    # whether it's accelerating (price_acceleration_score above already
    # covers that). Blends the freshest window (1m) with a short-term
    # confirmation window (5m) -- both snapshot-history derived and always
    # populated (unlike the newer Optional TICK-buffer return_* fields),
    # so this stays meaningful even for a just-discovered candidate with no
    # tick stream yet. Each window scored independently via scale3's
    # three-point progressive curve (min/strong/exceptional -- "barely
    # positive" and "screaming higher" read as meaningfully different
    # scores, not both maxing out past one threshold), then averaged into
    # one component the same way liquidity_score above blends spread +
    # dollar volume.
    price_momentum_1m_score = scale3(
        metrics.price_velocity_1m,
        th["price_momentum_1m_min"], th["price_momentum_1m_strong"], th["price_momentum_1m_exceptional"],
    )
    price_momentum_5m_score = scale3(
        metrics.price_velocity_5m,
        th["price_momentum_5m_min"], th["price_momentum_5m_strong"], th["price_momentum_5m_exceptional"],
    )
    price_momentum_score = (price_momentum_1m_score + price_momentum_5m_score) / 2

    # v2.6 addition (explicit user request): scores metrics.return_5m --
    # the same 5-minute regime metric/threshold
    # scanner/momentum_qualification.py's evaluate_trigger hard-gates
    # ARMED->CONFIRMING entries on (scoring/rtms_weights.yaml's
    # min_return_5m_pct) -- deliberately a different metric/threshold set
    # from price_momentum_5m_score above (metrics.price_velocity_5m).
    # momentum_regime_5m_exceptional is set equal to that same
    # min_return_5m_pct value in weights.yaml, so clearing the exact bar
    # RTMS later hard-gates on already maxes this component at 100 here,
    # not merely scale3's mid-curve value -- see
    # MomentumScoreComponents.momentum_regime_score's docstring. None
    # (excluded, not scored as 0) until metrics.return_5m itself is
    # available -- same contract as room_to_target_score/order_flow_score
    # above.
    momentum_regime_score = None
    if metrics.return_5m is not None:
        momentum_regime_score = scale3(
            metrics.return_5m,
            th["momentum_regime_5m_min"], th["momentum_regime_5m_strong"], th["momentum_regime_5m_exceptional"],
        )

    return MomentumScoreComponents(
        float_score=float_score,
        float_velocity_score=float_velocity_score,
        relative_volume_score=relative_volume_score,
        volume_acceleration_score=volume_acceleration_score,
        price_acceleration_score=price_acceleration_score,
        breakout_proximity_score=breakout_proximity_score,
        liquidity_score=liquidity_score,
        float_turnover_score=float_turnover_score,
        short_term_relative_volume_score=short_term_relative_volume_score,
        dollar_volume_acceleration_score=dollar_volume_acceleration_score,
        room_to_target_score=room_to_target_score,
        order_flow_score=order_flow_score,
        price_momentum_score=price_momentum_score,
        momentum_regime_score=momentum_regime_score,
    )


def compute_score(
    metrics: MomentumMetrics,
    float_data: Optional[FloatData],
    config: Optional[MISConfig] = None,
    *,
    current_price: Optional[float] = None,
    target_pct: Optional[float] = None,
    static_resistance_levels: Optional[Sequence[float]] = None,
) -> MomentumScore:
    config = config or MISConfig.load()
    components = compute_components(
        metrics, float_data, config,
        current_price=current_price, target_pct=target_pct, static_resistance_levels=static_resistance_levels,
    )
    # Skip any component the caller didn't give enough context to compute
    # (currently only room_to_target_score can be None -- see its docstring)
    # and renormalize over the remaining active weights, rather than
    # treating a missing component as a 0 that would silently drag every
    # candidate's score down whenever current_price/target_pct aren't passed.
    weighted_sum = 0.0
    active_weight = 0.0
    for key, weight in config.weights.items():
        value = getattr(components, key)
        if value is None:
            continue
        weighted_sum += value * weight
        active_weight += weight
    score = weighted_sum / active_weight if active_weight else 0.0
    return MomentumScore(
        symbol=metrics.symbol,
        timestamp=metrics.timestamp,
        score=_clamp(score),
        components=components,
        weights_version=config.version,
    )
