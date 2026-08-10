"""
Tests for metrics/opening_range.py -- specifically that the 9:30am
US/Eastern market-open conversion to UTC is correct across both the EDT
(summer) and EST (winter) halves of the year, since a hardcoded UTC offset
would silently be wrong for one of them.
"""
from datetime import datetime

from webull_bot.metrics.opening_range import compute_opening_range_high


def _bar(time_str: str, high: float, low: float | None = None, volume: float = 100) -> dict:
    return {"time": time_str, "high": str(high), "low": str(low if low is not None else high), "volume": str(volume)}


def test_finds_high_within_opening_range_during_edt():
    # 2026-08-03 falls in EDT (UTC-4) -- 9:30am ET == 13:30 UTC.
    bars = [
        _bar("2026-08-03T13:29:00.000+0000", high=5.0),   # just before open -- excluded
        _bar("2026-08-03T13:30:00.000+0000", high=6.0),   # at open -- included
        _bar("2026-08-03T13:33:00.000+0000", high=7.5),   # within the 5-min window -- the max
        _bar("2026-08-03T13:35:00.000+0000", high=9.0),   # exactly at window end -- excluded
        _bar("2026-08-03T13:40:00.000+0000", high=20.0),  # well after the window -- excluded
    ]
    result = compute_opening_range_high(bars, opening_range_minutes=5, now=datetime(2026, 8, 3, 15, 0, 0))
    assert result == 7.5


def test_finds_high_within_opening_range_during_est():
    # 2026-01-15 falls in EST (UTC-5) -- 9:30am ET == 14:30 UTC.
    bars = [
        _bar("2026-01-15T14:29:00.000+0000", high=5.0),  # before open -- excluded
        _bar("2026-01-15T14:31:00.000+0000", high=8.0),  # within window
    ]
    result = compute_opening_range_high(bars, opening_range_minutes=5, now=datetime(2026, 1, 15, 16, 0, 0))
    assert result == 8.0


def test_returns_none_when_no_bars_fall_in_the_window():
    bars = [_bar("2026-08-03T20:00:00.000+0000", high=10.0)]  # afternoon, well past the opening range
    result = compute_opening_range_high(bars, opening_range_minutes=5, now=datetime(2026, 8, 3, 21, 0, 0))
    assert result is None


def test_returns_none_for_empty_bars():
    assert compute_opening_range_high([], now=datetime(2026, 8, 3, 15, 0, 0)) is None


def test_uses_a_wider_window_when_configured():
    bars = [
        _bar("2026-08-03T13:34:00.000+0000", high=8.0),   # within a 15-min window, outside a 5-min one
        _bar("2026-08-03T13:44:00.000+0000", high=12.0),  # within a 15-min window -- the max
    ]
    result = compute_opening_range_high(bars, opening_range_minutes=15, now=datetime(2026, 8, 3, 15, 0, 0))
    assert result == 12.0
