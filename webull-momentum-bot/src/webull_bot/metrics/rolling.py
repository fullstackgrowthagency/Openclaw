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

from ..models import MarketSnapshot, MomentumMetrics, TickRecord
from ..enums import TradeSide
from .calculations import (
    bid_ask_spread,
    distance_pct,
    dollar_volume,
    dollar_volume_from_avg_price,
    float_velocity,
    mid_or_last,
    order_flow_imbalance,
    price_acceleration,
    price_range_pct,
    price_velocity_pct,
    relative_volume,
    trade_velocity,
    trend_efficiency,
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


def _order_flow_since(ticks: Sequence[TickRecord], now: datetime, minutes: float = 1.0) -> tuple[float, float, int, int]:
    """Sums TICK-derived trade volume by side within the trailing `minutes`
    window ending at `now` -- mirrors _volume_since's windowing, but counts
    individual trade prints rather than diffing cumulative_volume, since
    TickRecord.volume is each trade's own size, not a running total.
    Returns (buy_volume, sell_volume, classified_sample_count, total_tick_count):
    TradeSide.UNKNOWN ticks count toward neither buy nor sell volume nor
    classified_sample_count (see TickRecord's docstring for why a print
    can be unclassified) but are still real trades, so total_tick_count
    -- used for trade_velocity, which measures activity, not direction --
    counts every tick in the window regardless of side."""
    cutoff = now - timedelta(minutes=minutes)
    buy_volume = sell_volume = 0.0
    sample_count = 0
    total_count = 0
    for tick in ticks:
        if tick.timestamp < cutoff:
            continue
        total_count += 1
        if tick.side == TradeSide.BUY:
            buy_volume += tick.volume
            sample_count += 1
        elif tick.side == TradeSide.SELL:
            sell_volume += tick.volume
            sample_count += 1
    return buy_volume, sell_volume, sample_count, total_count


def _reference_price_at(
    ticks: Sequence[TickRecord], target_time: datetime,
    *, pad_seconds: float = 1.0, fallback_pad_seconds: float = 3.0,
) -> Optional[float]:
    """Price anchor for a historical window boundary (e.g. "the price
    ~15 seconds ago"), robust to a single abnormal trade print: the
    median of all trade prices within +/-pad_seconds of target_time, so
    one bad print among several nearby real ones can't skew the reading.
    Falls back to the single nearest print within fallback_pad_seconds
    when nothing landed inside the tighter pad_seconds window (tick
    activity is sparse right around target_time but not absent nearby).
    Returns None when nothing is close enough at all -- the tick buffer
    doesn't reach back this far yet (e.g. a candidate that only just
    started streaming) -- callers must treat None as "not measurable",
    never as 0."""
    close = [t.price for t in ticks if abs((t.timestamp - target_time).total_seconds()) <= pad_seconds]
    if close:
        ordered = sorted(close)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[mid]
        return (ordered[mid - 1] + ordered[mid]) / 2.0

    nearest: Optional[tuple[float, float]] = None  # (distance_seconds, price)
    for t in ticks:
        distance = abs((t.timestamp - target_time).total_seconds())
        if distance > fallback_pad_seconds:
            continue
        if nearest is None or distance < nearest[0]:
            nearest = (distance, t.price)
    return nearest[1] if nearest is not None else None


def _recent_window_high(
    ticks: Sequence[TickRecord], now: datetime, window_seconds: float = 15.0,
) -> tuple[Optional[float], Optional[datetime]]:
    """Highest trade print (and when it happened) among ticks in the
    trailing window_seconds -- feeds the IMPULSING quality gate's "recent
    high made within the last ~4s" freshness check and RTMS's
    fresh_high_reclaim_score (scanner/momentum_qualification.py,
    scoring/rtms.py). (None, None) when there's no tick data in the
    window yet."""
    cutoff = now - timedelta(seconds=window_seconds)
    best: Optional[tuple[float, datetime]] = None
    for t in ticks:
        if t.timestamp < cutoff:
            continue
        if best is None or t.price > best[0]:
            best = (t.price, t.timestamp)
    return (best[0], best[1]) if best is not None else (None, None)


def compute_metrics(
    free_float_shares: Optional[float],
    history: list[MarketSnapshot],
    *,
    typical_volume_same_time: Optional[float] = None,
    typical_volume_1m: Optional[float] = None,
    typical_volume_5m: Optional[float] = None,
    resistance_level: Optional[float] = None,
    # TICK-derived trade prints (see brokers/webull/client.py's
    # get_recent_ticks and TickRecord's docstring) -- optional, like every
    # other context-dependent input here (typical_volume_*, resistance_level):
    # None (the default) simply leaves order_flow_imbalance_1m/trade_velocity
    # uncomputed (None), exactly the contract every other optional metric
    # in this module already follows. Not part of `history`'s
    # MarketSnapshot rolling window since ticks are fundamentally a
    # different shape of data (individual trade prints, not periodic
    # aggregated state) -- see TickRecord's docstring.
    ticks: Optional[Sequence[TickRecord]] = None,
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

    # TICK-derived order flow -- see compute_metrics' `ticks` parameter
    # docstring and MomentumMetrics.order_flow_imbalance_1m's docstring for
    # why this stays None (not 0.0) whenever there's no classified volume
    # to measure, rather than defaulting to "confirmed balanced."
    ticks = ticks or []
    buy_volume_1m, sell_volume_1m, order_flow_sample_count_1m, total_ticks_1m = _order_flow_since(ticks, now, minutes=1.0)
    order_flow_imb_1m = (
        order_flow_imbalance(buy_volume_1m, sell_volume_1m) if (buy_volume_1m + sell_volume_1m) > 0 else None
    )
    trade_velocity_val = trade_velocity(total_ticks_1m, 60.0) if ticks else None

    # -- Real-Time Momentum Qualification Layer (2026-08-17) --------------
    # Short-window (5s/15s/30s/60s) returns/velocity/acceleration, derived
    # from the TICK buffer (not `history`'s snapshot windows -- see
    # MomentumMetrics.return_5s's docstring for why: the snapshot poll
    # cadence is too coarse on its own for these). mid_now uses the
    # midpoint-preferring reference price (mid_or_last) as the CURRENT
    # side of every return calc; historical boundaries use
    # _reference_price_at's own median-of-nearby-prints robustness
    # instead, since there's no historical bid/ask to take a midpoint of.
    mid_now = mid_or_last(latest.bid, latest.ask, latest.last_price)

    def _tick_return_velocity_accel(window_seconds: float) -> tuple[Optional[float], Optional[float], Optional[float]]:
        ref = _reference_price_at(ticks, now - timedelta(seconds=window_seconds))
        if ref is None:
            return None, None, None
        ret = price_velocity_pct(mid_now, ref)
        vel = ret / window_seconds
        prior_ref = _reference_price_at(ticks, now - timedelta(seconds=2 * window_seconds))
        if prior_ref is None:
            return ret, vel, None
        prior_ret = price_velocity_pct(ref, prior_ref)
        prior_vel = prior_ret / window_seconds
        return ret, vel, price_acceleration(vel, prior_vel)

    return_5s, velocity_5s, acceleration_5s = _tick_return_velocity_accel(5.0)
    return_15s, velocity_15s, acceleration_15s = _tick_return_velocity_accel(15.0)
    return_30s, velocity_30s, acceleration_30s = _tick_return_velocity_accel(30.0)
    return_60s, velocity_60s, acceleration_60s = _tick_return_velocity_accel(60.0)

    ticks_15s = sorted((t for t in ticks if t.timestamp >= now - timedelta(seconds=15)), key=lambda t: t.timestamp)
    ticks_60s = sorted((t for t in ticks if t.timestamp >= now - timedelta(seconds=60)), key=lambda t: t.timestamp)
    trend_eff_15s = trend_efficiency([t.price for t in ticks_15s])
    trend_eff_60s = trend_efficiency([t.price for t in ticks_60s])
    recent_high_15s, recent_high_15s_time = _recent_window_high(ticks, now, 15.0)

    # return_2m/3m/5m and trend_efficiency_5m: the tick buffer's ~90s
    # retention physically can't reach back this far, so these fall back
    # to the existing snapshot-history windows (w2 new, w3/w5 already
    # sliced above) -- using mid_or_last on BOTH boundaries, unlike the
    # legacy price_velocity_3m/5m fields above, which use raw last_price
    # and are left completely untouched by this addition.
    w2 = _window(history, now, 2)
    return_2m = price_velocity_pct(mid_now, mid_or_last(w2[0].bid, w2[0].ask, w2[0].last_price)) if w2 else None
    return_3m = price_velocity_pct(mid_now, mid_or_last(w3[0].bid, w3[0].ask, w3[0].last_price)) if w3 else None
    return_5m = price_velocity_pct(mid_now, mid_or_last(w5[0].bid, w5[0].ask, w5[0].last_price)) if w5 else None
    trend_eff_5m = trend_efficiency([mid_or_last(s.bid, s.ask, s.last_price) for s in w5])

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
        trade_velocity=trade_velocity_val,
        buy_volume_1m=buy_volume_1m,
        sell_volume_1m=sell_volume_1m,
        order_flow_imbalance_1m=order_flow_imb_1m,
        order_flow_sample_count_1m=order_flow_sample_count_1m,
        return_5s=return_5s,
        return_15s=return_15s,
        return_30s=return_30s,
        return_60s=return_60s,
        return_2m=return_2m,
        return_3m=return_3m,
        return_5m=return_5m,
        velocity_5s=velocity_5s,
        velocity_15s=velocity_15s,
        velocity_30s=velocity_30s,
        velocity_60s=velocity_60s,
        acceleration_5s=acceleration_5s,
        acceleration_15s=acceleration_15s,
        acceleration_30s=acceleration_30s,
        acceleration_60s=acceleration_60s,
        trend_efficiency_15s=trend_eff_15s,
        trend_efficiency_60s=trend_eff_60s,
        trend_efficiency_5m=trend_eff_5m,
        recent_high_15s=recent_high_15s,
        recent_high_15s_time=recent_high_15s_time,
    )
