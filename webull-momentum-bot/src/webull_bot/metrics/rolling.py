"""
Turns a rolling window of MarketSnapshots (+ free float) into a
MomentumMetrics record, using the pure functions in calculations.py.

`typical_volume_same_time`/`typical_volume_1m`/`typical_volume_5m` (RVOL
baselines) and `resistance_level` are not computed here on purpose:
resistance is a candidate-level concept owned by the scanner, and the RVOL
baselines are built once per candidate from Webull's own historical bars
(see metrics/volume_baseline.py and BroadScanner._compute_volume_baseline)
rather than accumulated from this bot's own observed history over time --
a low-float mover is often a symbol never watched before, so a baseline
that only builds up from this bot's own DB would have nothing to compare
against on exactly the day it matters most. All three are accepted as
optional inputs so callers can supply them without this module needing to
change; until supplied they default to None and the corresponding
relative-volume fields fall back to a neutral 1.0 (see relative_volume()'s
own safe-division default).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, Sequence

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


def _parse_bar_time(raw_time) -> datetime:
    if isinstance(raw_time, str):
        return datetime.strptime(raw_time, "%Y-%m-%dT%H:%M:%S.%f%z").replace(tzinfo=None)
    return raw_time


def seed_history_from_bars(
    bars: Sequence[dict], *, current: MarketSnapshot, lookback_minutes: float = MAX_HISTORY_MINUTES
) -> list[MarketSnapshot]:
    """Reconstructs synthetic MarketSnapshots for the `lookback_minutes`
    immediately before `current` from raw per-bar OHLCV (Webull's native
    shape, same input as metrics/volume_profile.py's compute_volume_profile),
    so CandidateWatcher's rolling window doesn't start completely blind to
    whatever momentum already happened just before this candidate was
    discovered.

    THE PROBLEM THIS SOLVES: a low-float momentum bot's candidates are
    discovered precisely BECAUSE they already made a move big enough to
    surface on a screener -- discovery structurally lags the move, not the
    other way around. Before this existed, CandidateWatcher._history started
    as an empty list at discovery, so float_velocity_5m/volume_accel_1m_3m/
    price_acceleration/relative_volume_5m/dollar_volume_accel_1m_3m (every
    metric that diffs across a rolling window, as opposed to reading an
    absolute cumulative total) read their cold-start neutral default for
    several real minutes after discovery, completely blind to a move that
    may have already happened minutes or hours earlier -- e.g. a name up
    +100% in pre-market on huge volume would show 0 across every "is this
    accelerating right now" component while its cumulative-total components
    (relative_volume, float_turnover, breakout_proximity) correctly read
    100, understating a candidate that's actually already extremely hot.

    ANCHORING: seeded cumulative_volume is NOT a fresh count starting at 0 --
    it's built backward from `current.cumulative_volume` (the real, true
    total as of discovery) so the seeded series and the live snapshot feed
    that starts arriving right after this share the exact same absolute
    scale. Without this, splicing a locally-reconstructed volume count
    (starting at 0) in front of the live feed's actual multi-million-share
    total would produce one enormous, meaningless synthetic "volume spike"
    at the seam between seed data and the first real tick. Concretely: the
    seed window's total bar volume is subtracted from current.cumulative_volume
    to get the running total AS OF the earliest seed bar, then each bar's own
    volume is added back in walking forward -- so the last seed snapshot's
    cumulative_volume lands just under current.cumulative_volume, and the
    live snapshot that follows continues that same series with no jump.

    Only `timestamp`/`cumulative_volume`/`last_price` are computed
    meaningfully -- compute_metrics only reads those three fields from
    non-latest history entries (see _window/_volume_since/price_velocity_pct
    above); bid/ask/high/low/vwap/open_price on a seed snapshot are set to
    last_price as an inert placeholder, never read for a historical entry.

    Bars from a different calendar day, or bars at/after `current`'s own
    timestamp, are excluded -- this seeds "recent run-up," not a multi-day
    history the way volume_baseline.py deliberately wants. A rolling window
    spanning the pre-market/regular-session boundary can still see one
    self-healing artificially-flat reading right at that seam, same known
    caveat as the ext_volume phase-reset handling in
    WebullBrokerClient._snapshot_from_dict -- not specially handled here."""
    if not bars:
        return []

    cutoff = current.timestamp - timedelta(minutes=lookback_minutes)
    same_day_recent = sorted(
        (bar for bar in bars if cutoff <= _parse_bar_time(bar["time"]) < current.timestamp),
        key=lambda bar: _parse_bar_time(bar["time"]),
    )
    if not same_day_recent:
        return []

    total_recent_volume = sum(float(bar["volume"]) for bar in same_day_recent)
    running = max(0.0, current.cumulative_volume - total_recent_volume)

    seeded: list[MarketSnapshot] = []
    for bar in same_day_recent:
        running += float(bar["volume"])
        price = float(bar["close"])
        seeded.append(
            MarketSnapshot(
                symbol=current.symbol,
                timestamp=_parse_bar_time(bar["time"]),
                last_price=price,
                bid=price,
                ask=price,
                bid_size=0.0,
                ask_size=0.0,
                cumulative_volume=running,
                vwap=price,
                high_of_day=price,
                low_of_day=price,
                open_price=price,
            )
        )
    return seeded


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
