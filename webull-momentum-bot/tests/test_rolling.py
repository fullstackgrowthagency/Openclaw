"""
Tests for metrics/rolling.py's compute_metrics, focused on the windowed
volume/dollar-volume fields added alongside the removal of BroadScanner's
hard dollar-volume/average-volume rejection gates -- these are now the
metrics that scoring/diagnostics lean on instead of a pass/fail cutoff.
"""
from datetime import datetime, timedelta

from webull_bot.metrics.rolling import compute_metrics
from webull_bot.models import MarketSnapshot


def _snap(minutes_ago, price, cumulative_volume, now):
    t = now - timedelta(minutes=minutes_ago)
    return MarketSnapshot(
        symbol="TEST", timestamp=t, last_price=price, bid=price - 0.01, ask=price + 0.01,
        bid_size=100, ask_size=100, cumulative_volume=cumulative_volume, vwap=price,
        high_of_day=price, low_of_day=price, open_price=price,
    )


def test_compute_metrics_requires_at_least_one_snapshot():
    import pytest
    with pytest.raises(ValueError):
        compute_metrics(1_000_000, [])


def test_volume_windows_reflect_cumulative_volume_deltas():
    now = datetime(2026, 1, 1, 10, 0, 0)
    history = [
        _snap(15, 10.0, 0, now),
        _snap(5, 10.0, 100_000, now),
        _snap(1, 10.0, 150_000, now),
        _snap(0, 10.0, 200_000, now),
    ]
    m = compute_metrics(2_000_000, history)
    assert m.volume_1m == 50_000    # 200_000 - 150_000
    assert m.volume_5m == 100_000   # 200_000 - 100_000
    assert m.volume_15m == 200_000  # 200_000 - 0


def test_dollar_volume_windows_use_boundary_price_average():
    now = datetime(2026, 1, 1, 10, 0, 0)
    history = [
        _snap(1, 10.0, 100_000, now),
        _snap(0, 12.0, 150_000, now),
    ]
    m = compute_metrics(2_000_000, history)
    # volume_1m = 50_000 shares, boundary prices 10.0 -> 12.0, avg = 11.0
    assert m.volume_1m == 50_000
    assert m.dollar_volume_1m == 50_000 * 11.0


def test_dollar_volume_acceleration_differs_from_share_volume_acceleration_when_price_moves():
    now = datetime(2026, 1, 1, 10, 0, 0)
    # Price is rising: 3m-ago 10.0 -> 1m-ago 11.0 -> now 13.0. Share volume
    # rate is flat (same shares/min each segment), but dollar rate should
    # still show acceleration purely from the price increase.
    history = [
        _snap(3, 10.0, 0, now),
        _snap(1, 11.0, 100_000, now),   # 100_000 shares over the preceding 2 min segment
        _snap(0, 13.0, 150_000, now),   # 50_000 shares over the last 1 min
    ]
    m = compute_metrics(2_000_000, history)
    assert m.volume_accel_1m_3m == 1.0  # 50_000/min recent vs 50_000/min preceding -- flat
    assert m.dollar_volume_accel_1m_3m > 1.0  # price rose, so dollar rate accelerated even though share rate didn't


def test_relative_volume_windows_default_to_neutral_without_a_baseline():
    now = datetime(2026, 1, 1, 10, 0, 0)
    history = [_snap(1, 10.0, 100_000, now), _snap(0, 10.0, 150_000, now)]
    m = compute_metrics(2_000_000, history)
    assert m.relative_volume_1m == 1.0
    assert m.relative_volume_5m == 1.0


def test_relative_volume_windows_use_provided_baseline():
    now = datetime(2026, 1, 1, 10, 0, 0)
    history = [_snap(1, 10.0, 100_000, now), _snap(0, 10.0, 150_000, now)]
    m = compute_metrics(2_000_000, history, typical_volume_1m=25_000)
    assert m.relative_volume_1m == 2.0  # 50_000 actual / 25_000 typical


def test_float_velocity_5m_is_5_minute_float_turnover():
    now = datetime(2026, 1, 1, 10, 0, 0)
    history = [_snap(5, 10.0, 0, now), _snap(0, 10.0, 300_000, now)]
    m = compute_metrics(free_float_shares=1_000_000, history=history)
    assert m.float_velocity_5m == 0.3  # 300_000 / 1_000_000
