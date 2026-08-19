"""
Real-Time Momentum Score (RTMS): a configurable 0-100 score measuring how
strongly a candidate is moving upward RIGHT NOW -- deliberately separate
from the Momentum Ignition Score (scoring/momentum_ignition_score.py),
which measures whether a candidate is structurally interesting/capable of
producing momentum, not whether it's actually doing so at this instant.

Added 2026-08-17 as part of the Real-Time Momentum Qualification Layer
(scanner/momentum_qualification.py) -- see that module for exactly where
RTMS is computed and gated (entry threshold to start CONFIRMING, a looser
threshold at the final pre-submission recheck) and MomentumState.rtms for
where the result is stored per-candidate.

Structurally a direct copy of momentum_ignition_score.py's pattern
(MISConfig/compute_components/compute_score): yaml-driven weights,
missing components excluded and the remaining weights renormalized rather
than treated as 0. The one real difference is the scoring curve shape --
RTMS's components use a three-point (min/strong/exceptional) progressive
curve (metrics/calculations.py's scale3, also reused by MIS's own
price_momentum_score) instead of MIS's plain two-point _scale, so "barely
qualifies" and "extremely strong" read as meaningfully different scores.

All weights/thresholds live in rtms_weights.yaml so they can be tuned from
paper-trading results without code changes, exactly like weights.yaml.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from ..metrics.calculations import scale3
from ..models import MomentumMetrics, RTMSComponents, RTMSScore

_DEFAULT_WEIGHTS_PATH = Path(__file__).parent / "rtms_weights.yaml"


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def _fresh_high_reclaim_score(
    current_price: Optional[float],
    recent_high_15s: Optional[float],
    recent_high_15s_time: Optional[datetime],
    now: datetime,
    curve: dict,
    *,
    mid: float = 60.0,
) -> Optional[float]:
    """Descending-seconds special case (see rtms_weights.yaml's own
    comment on this curve): None when there's no recent-high context to
    measure against; 0.0 once current price has fallen more than
    proximity_pct below that high (not "fresh" in any useful sense
    regardless of timing); otherwise a three-point curve over
    seconds-since-the-high, where FEWER seconds scores HIGHER (a high made
    2 seconds ago is much more meaningful than one made 8 seconds ago)."""
    if recent_high_15s is None or recent_high_15s_time is None or recent_high_15s <= 0 or current_price is None:
        return None
    distance_pct = (recent_high_15s - current_price) / recent_high_15s * 100.0
    if distance_pct > curve["proximity_pct"]:
        return 0.0
    seconds_since = max(0.0, (now - recent_high_15s_time).total_seconds())
    min_s, strong_s, exceptional_s = curve["min_seconds"], curve["strong_seconds"], curve["exceptional_seconds"]
    if seconds_since >= min_s:
        return 0.0
    if seconds_since <= exceptional_s:
        return 100.0
    if seconds_since >= strong_s:
        span = min_s - strong_s
        frac = (min_s - seconds_since) / span if span else 1.0
        return _clamp(frac * mid)
    span = strong_s - exceptional_s
    frac = (strong_s - seconds_since) / span if span else 1.0
    return _clamp(mid + frac * (100.0 - mid))


@dataclass(frozen=True)
class RTMSConfig:
    version: str
    weights: dict[str, float]
    component_curves: dict[str, dict]
    thresholds: dict[str, float]
    ranking_weights: dict[str, float]
    strategy_phase_policy: dict[str, frozenset]

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "RTMSConfig":
        from ..enums import MomentumPhase

        path = path or _DEFAULT_WEIGHTS_PATH
        raw = yaml.safe_load(path.read_text())
        weights = dict(raw["weights"])
        total = sum(weights.values()) or 1.0
        normalized = {k: v / total for k, v in weights.items()}
        phase_policy = {
            strategy_name: frozenset(MomentumPhase(p) for p in phases)
            for strategy_name, phases in raw["strategy_phase_policy"].items()
        }
        return cls(
            version=raw["version"],
            weights=normalized,
            component_curves=dict(raw["component_curves"]),
            thresholds=dict(raw["thresholds"]),
            ranking_weights=dict(raw["ranking_weights"]),
            strategy_phase_policy=phase_policy,
        )


def compute_rtms_components(
    metrics: Optional[MomentumMetrics],
    config: RTMSConfig,
    now: datetime,
    *,
    current_price: Optional[float] = None,
) -> RTMSComponents:
    if metrics is None:
        return RTMSComponents(
            regime_distance_score=None,
            momentum_15s_score=None, momentum_30s_score=None, momentum_60s_score=None,
            price_acceleration_score=None, trend_efficiency_score=None,
            fresh_high_reclaim_score=None, order_flow_trade_velocity_score=None,
            volume_acceleration_score=None,
        )
    curves = config.component_curves
    th = config.thresholds

    def _curve(name: str, value: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        c = curves[name]
        return scale3(value, c["min"], c["strong"], c["exceptional"])

    # rtms-v4 (2026-08-19): distance ABOVE the hard entry gate
    # (min_return_5m_pct), not return_5m itself -- scored via the same
    # three-point curve as everything else, over "percentage points
    # cleared past the floor" rather than the raw return, so this stays
    # meaningful even if min_return_5m_pct itself gets retuned later.
    regime_distance_score = None
    if metrics.return_5m is not None:
        distance_above_floor = metrics.return_5m - th["min_return_5m_pct"]
        regime_distance_score = _curve("regime_distance_score", distance_above_floor)

    momentum_15s_score = _curve("momentum_15s_score", metrics.return_15s)
    momentum_30s_score = _curve("momentum_30s_score", metrics.return_30s)
    momentum_60s_score = _curve("momentum_60s_score", metrics.return_60s)
    price_acceleration_score = _curve("price_acceleration_score", metrics.acceleration_15s)
    trend_efficiency_score = _curve("trend_efficiency_score", metrics.trend_efficiency_15s)

    fresh_high_reclaim_score = _fresh_high_reclaim_score(
        current_price, metrics.recent_high_15s, metrics.recent_high_15s_time, now,
        curves["fresh_high_reclaim_score"],
    )

    # Same TICK-derived buy/sell aggressor signal MIS's own order_flow_score
    # reads (metrics.order_flow_imbalance_1m) -- not recomputed here, just
    # reused, gated behind the same "enough classified prints to trust it"
    # sample-count floor.
    order_flow_trade_velocity_score = None
    if (
        metrics.order_flow_imbalance_1m is not None
        and metrics.order_flow_sample_count_1m >= th["min_order_flow_sample_count"]
    ):
        c = curves["order_flow_trade_velocity_score"]
        order_flow_trade_velocity_score = scale3(metrics.order_flow_imbalance_1m, c["min"], c["strong"], c["exceptional"])

    volume_acceleration_score = _curve("volume_acceleration_score", metrics.volume_accel_1m_3m)

    return RTMSComponents(
        regime_distance_score=regime_distance_score,
        momentum_15s_score=momentum_15s_score,
        momentum_30s_score=momentum_30s_score,
        momentum_60s_score=momentum_60s_score,
        price_acceleration_score=price_acceleration_score,
        trend_efficiency_score=trend_efficiency_score,
        fresh_high_reclaim_score=fresh_high_reclaim_score,
        order_flow_trade_velocity_score=order_flow_trade_velocity_score,
        volume_acceleration_score=volume_acceleration_score,
    )


def compute_rtms(
    metrics: Optional[MomentumMetrics],
    config: Optional[RTMSConfig] = None,
    now: Optional[datetime] = None,
    *,
    current_price: Optional[float] = None,
) -> tuple[float, RTMSComponents]:
    """Returns (score, components) rather than an RTMSScore dataclass --
    unlike MIS (computed once per candidate per tick and stored directly
    as Candidate.latest_score), RTMS is computed and consulted repeatedly
    within the same tick by scanner/momentum_qualification.py, which
    already owns where the result gets stored (MomentumState.rtms/
    rtms_components) -- returning the tuple directly avoids constructing
    and immediately unpacking a wrapper object at every call site."""
    config = config or RTMSConfig.load()
    now = now or (metrics.timestamp if metrics is not None else datetime.utcnow())
    components = compute_rtms_components(metrics, config, now, current_price=current_price)

    weighted_sum = 0.0
    active_weight = 0.0
    for key, weight in config.weights.items():
        value = getattr(components, key)
        if value is None:
            continue
        weighted_sum += value * weight
        active_weight += weight
    score = weighted_sum / active_weight if active_weight else 0.0
    return _clamp(score), components


def build_rtms_score(metrics: MomentumMetrics, score: float, components: RTMSComponents, config: RTMSConfig) -> RTMSScore:
    """Optional convenience wrapper for callers (e.g. persistence/dashboard
    code) that want the same RTMSScore shape MomentumScore already has,
    rather than the raw (score, components) tuple compute_rtms returns."""
    return RTMSScore(
        symbol=metrics.symbol, timestamp=metrics.timestamp, score=score,
        components=components, weights_version=config.version,
    )
