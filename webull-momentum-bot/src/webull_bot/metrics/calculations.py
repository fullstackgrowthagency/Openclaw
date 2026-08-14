"""
Pure functions for all the momentum/liquidity metrics from the project
outline. Kept side-effect-free and broker-agnostic so they're trivially
unit-testable and reusable from both the live pipeline and the backtester.

Naming convention: functions take plain numbers/sequences, never Candidate
or MarketSnapshot objects directly -- see metrics/features.py (future work)
for the glue that pulls fields off those objects and calls into here.
"""
from __future__ import annotations

from typing import Sequence


def safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    if denominator in (0, 0.0, None):
        return default
    return numerator / denominator


def float_turnover(cumulative_volume: float, free_float_shares: float) -> float:
    """Cumulative session volume as a multiple of the free float."""
    return safe_div(cumulative_volume, free_float_shares)


def float_velocity(window_volume: float, free_float_shares: float) -> float:
    """Volume traded in a short window (e.g. last 1/3/5 min) as a fraction of free float."""
    return safe_div(window_volume, free_float_shares)


def relative_volume(current_volume: float, typical_volume_same_time: float) -> float:
    """RVOL: current cumulative/window volume vs. the stock's historical norm
    at the same time of day. >1 means more active than usual."""
    return safe_div(current_volume, typical_volume_same_time, default=1.0)


def volume_acceleration(recent_window_rate: float, prior_window_rate: float) -> float:
    """Ratio of the most recent volume rate (shares/min) to the immediately
    preceding rate. >1 means volume is accelerating."""
    return safe_div(recent_window_rate, prior_window_rate, default=1.0)


def volume_rate(volumes: Sequence[float], minutes: float) -> float:
    """Shares/minute over a window, given a list of per-bar volumes."""
    return safe_div(sum(volumes), minutes)


def price_velocity_pct(price_now: float, price_then: float) -> float:
    """% price change over a lookback window."""
    return safe_div(price_now - price_then, price_then) * 100.0


def price_acceleration(velocity_now_pct: float, velocity_prior_pct: float) -> float:
    """Change in price velocity itself -- is momentum speeding up or slowing down."""
    return velocity_now_pct - velocity_prior_pct


def vwap(cumulative_price_volume: float, cumulative_volume: float) -> float:
    return safe_div(cumulative_price_volume, cumulative_volume)


def rolling_vwap(prices: Sequence[float], volumes: Sequence[float]) -> float:
    pv = sum(p * v for p, v in zip(prices, volumes))
    return safe_div(pv, sum(volumes))


def distance_pct(current_price: float, reference_price: float) -> float:
    """% distance of current price from a reference level (VWAP, HOD, resistance, etc.).
    Positive means current price is above the reference."""
    return safe_div(current_price - reference_price, reference_price) * 100.0


def bid_ask_spread(bid: float, ask: float) -> tuple[float, float]:
    """Returns (absolute spread, spread as % of mid price)."""
    if bid <= 0 or ask <= 0 or ask < bid:
        return (0.0, 0.0)
    spread = ask - bid
    mid = (ask + bid) / 2
    return (spread, safe_div(spread, mid) * 100.0)


def dollar_volume(price: float, volume: float) -> float:
    return price * volume


def dollar_volume_from_avg_price(volume: float, price_start: float, price_end: float) -> float:
    """Dollar volume over a window, approximated using the average of the
    window's boundary prices (its start and end snapshot prices) -- the
    best estimate available given only boundary-snapshot granularity, not
    true tick-level VWAP for the window. Deliberately distinct from
    dollar_volume() above (which uses a single current price): using two
    different boundary prices means a dollar-volume comparison across
    windows can differ from a plain share-volume comparison when price
    itself moved between windows, which is the whole point of tracking
    dollar-volume acceleration as separate from share-volume acceleration
    -- see metrics/rolling.py's compute_metrics."""
    avg_price = (price_start + price_end) / 2.0
    return volume * avg_price


def trade_velocity(trade_count: int, seconds: float) -> float:
    """Trades per second over a short window -- requires tick/trade data."""
    return safe_div(trade_count, seconds)


def order_flow_imbalance(buy_volume: float, sell_volume: float) -> float:
    """(buy - sell) / (buy + sell): -1.0 (entirely seller-initiated) to
    +1.0 (entirely buyer-initiated), 0.0 for a perfectly balanced split OR
    (via safe_div's own default) when there's no classified volume at all
    to measure. That ambiguity is deliberate and matches relative_volume's
    own default=1.0 pattern: this pure function always returns a definite
    number, and it's the CALLER's job to check buy_volume + sell_volume > 0
    before trusting a 0.0 result as "confirmed balanced" rather than
    "nothing to measure" -- see MomentumMetrics.order_flow_imbalance_1m,
    which is None (not 0.0) in exactly that case for this reason."""
    return safe_div(buy_volume - sell_volume, buy_volume + sell_volume, default=0.0)


def price_range_pct(prices: Sequence[float]) -> float:
    """High-low range of a list of prices as a % of their average -- a
    coarse volatility proxy for detecting a tightening/consolidating range
    (see strategy/volatility_contraction.py). Deliberately built from
    snapshot-level last_price samples, not true intra-window OHLC (Webull's
    snapshot API doesn't expose that at sub-minute granularity), consistent
    with how price_velocity_pct/price_acceleration above already operate
    on boundary snapshot prices rather than tick data. Returns 0.0 for an
    empty or single-price window (no range to measure)."""
    if not prices:
        return 0.0
    high, low = max(prices), min(prices)
    avg = sum(prices) / len(prices)
    return safe_div(high - low, avg) * 100.0
