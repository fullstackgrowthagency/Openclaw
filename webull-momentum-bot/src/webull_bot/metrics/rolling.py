"""
Turns a rolling window of MarketSnapshots (+ free float) into a
MomentumMetrics record, using the pure functions in calculations.py.

`typical_volume_same_time`/`typical_volume_1m`/`typical_volume_5m` (RVOL
baselines) and `resistance_level` are not computed here on purpose -- RVOL
needs a historical intraday volume profile per symbol (future work once
enough MarketObservation history has been collected in the DB), and
resistance is a candidate-level concept owned by the scanner. All three are
accepted as optional inputs so callers can supply them once those pieces
exist, without this module needing to change; until then they default to
None and the corresponding relative-volume fields fall back to a neutral
1.0 (see relative_volume()'s own safe-division default), same as the
existing whole-session relative_volume already does.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from ..models import MarketSnapshot, MomentumMetrics
from .calculations import (
    bid_ask_spread,
    distance_pct,
    dollar_volume,
    dollar_volume_from_avg_price,
    float_velocity,
    price_acceleration,
    price_range_pct,
    price_velocity_pct,
    relative_volume,
    volume_acceleration,
)

# A rolling history should hold at least this much look-back to compute all windows.
MAX_HISTORY_MINUTES = 20


def _window(history: list[MarketSnapshot], now: datetime, minutes: float) -> list[MarketSnapshot]:
    cutoff = now - timedelta(minutes=minutes)
    return [s for s in history if s.timestamp >= cutoff]


def _volume_since(history_window: list[MarketSnapshot], latest: MarketSnapshot) -> float:
    if not history_window:
        return 0.0
    return max(0.0, latest.cumulative_volume - history_window[0].cumulative_volume)


def compute_metrics(
    free_float_shares: Optional[float],
    history: list[MarketSnapshot],
    *,
    typical_volume_same_time: Optional[float] = None,
    typical_volume_1m: Optional[float] = None,
    typical_volume_5m: Optional[float] = None,
    resistance_level: Optional[float] = None,
) -> MomentumMetrics:
    if not history:
        raise ValueError("compute_metrics requires at least one snapshot")

    latest = history[-1]
    now = latest.timestamp

    w1 = _window(history, now, 1)
    w3 = _window(history, now, 3)
    w5 = _window(history, now, 5)
    w15 = _window(history, now, 15)

    vol_1m = _volume_since(w1, latest)
    vol_3m = _volume_since(w3, latest)
    vol_5m = _volume_since(w5, latest)
    vol_15m = _volume_since(w15, latest)

    free_float = free_float_shares or 0.0
    float_velocity_1m = float_velocity(vol_1m, free_float)
    float_velocity_3m = float_velocity(vol_3m, free_float)
    float_velocity_5m = float_velocity(vol_5m, free_float)

    recent_rate = vol_1m  # shares/min over the most recent minute
    preceding_volume = max(0.0, vol_3m - vol_1m)
    preceding_rate = preceding_volume / 2.0  # shares/min over the preceding 2 minutes
    vol_accel = volume_acceleration(recent_rate, preceding_rate)

    price_velocity_1m = price_velocity_pct(latest.last_price, w1[0].last_price) if w1 else 0.0
    price_velocity_3m = price_velocity_pct(latest.last_price, w3[0].last_price) if w3 else 0.0
    price_velocity_5m = price_velocity_pct(latest.last_price, w5[0].last_price) if w5 else 0.0
    price_velocity_15m = price_velocity_pct(latest.last_price, w15[0].last_price) if w15 else 0.0

    prior_segment_velocity = (
        price_velocity_pct(w1[0].last_price, w3[0].last_price) if (w1 and w3) else 0.0
    )
    price_accel = price_acceleration(price_velocity_1m, prior_segment_velocity)

    # Dollar volume per window, using each window's own boundary-price
    # average (dollar_volume_from_avg_price) rather than a single current
    # price -- see that function's docstring for why this matters for
    # dollar_volume_accel_1m_3m below to be a genuinely distinct signal
    # from volume_accel_1m_3m, not just a rescaled duplicate of it.
    dollar_vol_1m = dollar_volume_from_avg_price(vol_1m, w1[0].last_price, latest.last_price) if w1 else 0.0
    dollar_vol_5m = dollar_volume_from_avg_price(vol_5m, w5[0].last_price, latest.last_price) if w5 else 0.0
    dollar_vol_15m = dollar_volume_from_avg_price(vol_15m, w15[0].last_price, latest.last_price) if w15 else 0.0

    preceding_dollar_volume = (
        dollar_volume_from_avg_price(preceding_volume, w3[0].last_price, w1[0].last_price) if (w1 and w3) else 0.0
    )
    dollar_recent_rate = dollar_vol_1m
    dollar_preceding_rate = preceding_dollar_volume / 2.0
    dollar_vol_accel = volume_acceleration(dollar_recent_rate, dollar_preceding_rate)

    rvol = relative_volume(latest.cumulative_volume, typical_volume_same_time or 0.0)
    rvol_1m = relative_volume(vol_1m, typical_volume_1m or 0.0)
    rvol_5m = relative_volume(vol_5m, typical_volume_5m or 0.0)

    spread_abs, spread_pct = bid_ask_spread(latest.bid, latest.ask)

    # Reuses w3/w15 (already sliced above for price_velocity) rather than
    # adding new windows -- see strategy/volatility_contraction.py for how
    # the ratio between these two detects a recent range contraction.
    range_pct_3m = price_range_pct([s.last_price for s in w3])
    range_pct_15m = price_range_pct([s.last_price for s in w15])

    return MomentumMetrics(
        symbol=latest.symbol,
        timestamp=now,
        float_turnover=float_velocity(latest.cumulative_volume, free_float),
        float_velocity_1m=float_velocity_1m,
        float_velocity_3m=float_velocity_3m,
        float_velocity_5m=float_velocity_5m,
        relative_volume=rvol,
        relative_volume_1m=rvol_1m,
        relative_volume_5m=rvol_5m,
        volume_accel_1m_3m=vol_accel,
        volume_1m=vol_1m,
        volume_5m=vol_5m,
        volume_15m=vol_15m,
        dollar_volume_1m=dollar_vol_1m,
        dollar_volume_5m=dollar_vol_5m,
        dollar_volume_15m=dollar_vol_15m,
        dollar_volume_accel_1m_3m=dollar_vol_accel,
        price_velocity_1m=price_velocity_1m,
        price_velocity_3m=price_velocity_3m,
        price_velocity_5m=price_velocity_5m,
        price_velocity_15m=price_velocity_15m,
        price_acceleration=price_accel,
        vwap=latest.vwap,
        distance_from_vwap_pct=distance_pct(latest.last_price, latest.vwap) if latest.vwap else 0.0,
        distance_from_hod_pct=distance_pct(latest.last_price, latest.high_of_day) if latest.high_of_day else 0.0,
        distance_from_premarket_high_pct=(
            distance_pct(latest.last_price, latest.premarket_high) if latest.premarket_high else None
        ),
        distance_from_resistance_pct=(
            distance_pct(latest.last_price, resistance_level) if resistance_level else None
        ),
        spread_abs=spread_abs,
        spread_pct=spread_pct,
        dollar_volume=dollar_volume(latest.last_price, latest.cumulative_volume),
        price_range_pct_3m=range_pct_3m,
        price_range_pct_15m=range_pct_15m,
    )
