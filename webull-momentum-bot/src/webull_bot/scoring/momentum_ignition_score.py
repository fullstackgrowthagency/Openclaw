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
from typing import Optional

import yaml

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

    # Trend quality: being above VWAP and trending up is "healthy" momentum.
    trend_quality_score = _scale(metrics.distance_from_vwap_pct, -2.0, 5.0)

    # Liquidity: tight spread + healthy dollar volume.
    spread_score = 100.0 - _scale(metrics.spread_pct, 0.0, th["max_spread_pct"])
    dollar_volume_score = _scale(metrics.dollar_volume, th["min_dollar_volume"] * 0.25, th["min_dollar_volume"] * 4)
    liquidity_score = (spread_score + dollar_volume_score) / 2

    return MomentumScoreComponents(
        float_score=float_score,
        float_velocity_score=float_velocity_score,
        relative_volume_score=relative_volume_score,
        volume_acceleration_score=volume_acceleration_score,
        price_acceleration_score=price_acceleration_score,
        breakout_proximity_score=breakout_proximity_score,
        trend_quality_score=trend_quality_score,
        liquidity_score=liquidity_score,
        float_turnover_score=float_turnover_score,
        short_term_relative_volume_score=short_term_relative_volume_score,
        dollar_volume_acceleration_score=dollar_volume_acceleration_score,
    )


def compute_score(
    metrics: MomentumMetrics,
    float_data: Optional[FloatData],
    config: Optional[MISConfig] = None,
) -> MomentumScore:
    config = config or MISConfig.load()
    components = compute_components(metrics, float_data, config)
    weighted_sum = sum(getattr(components, key) * weight for key, weight in config.weights.items())
    return MomentumScore(
        symbol=metrics.symbol,
        timestamp=metrics.timestamp,
        score=_clamp(weighted_sum),
        components=components,
        weights_version=config.version,
    )
