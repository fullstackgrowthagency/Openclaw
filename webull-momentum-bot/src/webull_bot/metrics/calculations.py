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


def trade_velocity(trade_count: int, seconds: float) -> float:
    """Trades per second over a short window -- requires tick/trade data."""
    return safe_div(trade_count, seconds)
