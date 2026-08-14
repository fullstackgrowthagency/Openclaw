"""
Tests for metrics/session_vwap.py's compute_session_vwap_anchor -- the
discovery-time starting point for TradingLoop's real running session VWAP
(see runtime/trading_loop.py's _update_session_vwap and this module's own
docstring for the 2026-08-14 ONFO incident this whole feature fixes).
"""
from datetime import datetime

from webull_bot.metrics.session_vwap import compute_session_vwap_anchor


def _bar(close, volume, time):
    return {"time": time, "close": str(close), "volume": str(volume)}


def test_returns_none_none_for_no_bars():
    assert compute_session_vwap_anchor([], now=datetime(2026, 8, 14, 15, 0, 0)) == (None, None)


def test_sums_price_times_volume_across_todays_session_bars():
    # 2026-08-14 is EDT (UTC-4) -- 9:30am ET == 13:30 UTC.
    now = datetime(2026, 8, 14, 15, 0, 0)  # 11:00am ET
    bars = [
        _bar(3.0, 2000, "2026-08-14T13:35:00.000+0000"),  # 9:35am ET -- in session
        _bar(4.0, 1000, "2026-08-14T14:00:00.000+0000"),  # 10:00am ET -- in session
    ]
    pv, volume = compute_session_vwap_anchor(bars, now=now)
    assert pv == 3.0 * 2000 + 4.0 * 1000
    assert volume == 3000.0


def test_excludes_bars_before_market_open():
    now = datetime(2026, 8, 14, 15, 0, 0)
    bars = [_bar(2.0, 5000, "2026-08-14T13:25:00.000+0000")]  # 9:25am ET -- before open
    assert compute_session_vwap_anchor(bars, now=now) == (None, None)


def test_excludes_bars_after_now():
    now = datetime(2026, 8, 14, 14, 0, 0)  # 10:00am ET
    bars = [
        _bar(3.0, 1000, "2026-08-14T13:35:00.000+0000"),  # 9:35am ET -- included
        _bar(9.0, 9999, "2026-08-14T15:00:00.000+0000"),  # 11:00am ET -- future, excluded
    ]
    pv, volume = compute_session_vwap_anchor(bars, now=now)
    assert pv == 3.0 * 1000
    assert volume == 1000.0


def test_excludes_bars_from_a_previous_day():
    now = datetime(2026, 8, 14, 15, 0, 0)
    bars = [
        _bar(2.0, 5000, "2026-08-13T14:00:00.000+0000"),  # yesterday
        _bar(3.0, 1000, "2026-08-14T13:35:00.000+0000"),  # today
    ]
    pv, volume = compute_session_vwap_anchor(bars, now=now)
    assert pv == 3.0 * 1000
    assert volume == 1000.0
