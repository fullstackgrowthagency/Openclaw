"""
RSI (Wilder's smoothing) over a plain price series -- same "series aligned
with input, leading Nones" convention as moving_average.py.
"""
from __future__ import annotations

from typing import Optional


def rsi(prices: list[float], period: int = 14) -> list[Optional[float]]:
    if period < 1:
        raise ValueError("period must be >= 1")
    result: list[Optional[float]] = [None] * len(prices)
    if len(prices) <= period:
        return result

    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    result[period] = _rsi_from_averages(avg_gain, avg_loss)

    for i in range(period, len(deltas)):
        # Wilder's smoothing: each new average is a weighted blend of the
        # prior average and the latest single gain/loss, not a plain
        # rolling-window recompute.
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        result[i + 1] = _rsi_from_averages(avg_gain, avg_loss)

    return result


def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))
