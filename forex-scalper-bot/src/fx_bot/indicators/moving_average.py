"""
SMA/EMA over a plain price series (e.g. [snapshot.mid for snapshot in
history] -- see registry.py). Every indicator function in this package
returns a series the SAME LENGTH as its input, with leading `None`s
wherever there isn't yet enough history to compute a value -- this is
what lets the rule-builder compiler compare "current vs previous" for
crossover conditions (crosses_above/crosses_below) uniformly across any
indicator, not just look at a single latest value.
"""
from __future__ import annotations

from typing import Optional


def sma(prices: list[float], period: int) -> list[Optional[float]]:
    if period < 1:
        raise ValueError("period must be >= 1")
    result: list[Optional[float]] = [None] * len(prices)
    for i in range(period - 1, len(prices)):
        window = prices[i - period + 1: i + 1]
        result[i] = sum(window) / period
    return result


def ema(prices: list[float], period: int) -> list[Optional[float]]:
    if period < 1:
        raise ValueError("period must be >= 1")
    result: list[Optional[float]] = [None] * len(prices)
    if len(prices) < period:
        return result
    multiplier = 2.0 / (period + 1)
    # Seeded with the SMA of the first `period` prices, the standard
    # convention -- an EMA needs SOME starting value, and an unweighted
    # average of the first window is the usual choice.
    seed = sum(prices[:period]) / period
    result[period - 1] = seed
    previous = seed
    for i in range(period, len(prices)):
        previous = (prices[i] - previous) * multiplier + previous
        result[i] = previous
    return result
