"""
Tests for metrics/rolling.py's compute_metrics, focused on the windowed
volume/dollar-volume fields added alongside the removal of BroadScanner's
hard dollar-volume/average-volume rejection gates -- these are now the
metrics that scoring/diagnostics lean on instead of a pass/fail cutoff.
"""
from datetime import datetime, timedelta

from webull_bot.enums import TradeSide
from webull_bot.metrics.rolling import compute_metrics, seed_history_from_bars
from webull_bot.models import MarketSnapshot, TickRecord


def _snap(minutes_ago, price, cumulative_volume, now):
    t = now - timedelta(minutes=minutes_ago)
    return MarketSnapshot(
        symbol="TEST", timestamp=t, last_price=price, bid=price - 0.01, ask=price + 0.01,
        bid_size=100, ask_size=100, cumulative_volume=cumulative_volume, vwap=price,
        high_of_day=price, low_of_day=price, open_price=price,
    )


def _bar(volume, close, time):
    return {"time": time, "close": str(close), "volume": str(volume)}


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


# -- seed_history_from_bars (see its docstring for why this exists: discovery
# structurally lags the move that caused it, so CandidateWatcher's rolling
# window shouldn't start completely blind to what already happened) --------

def test_seed_history_from_bars_empty_bars_returns_empty():
    current = _snap(0, 10.0, 1000.0, datetime(2026, 8, 10, 13, 30))
    assert seed_history_from_bars([], current=current) == []


def test_seed_history_from_bars_excludes_bars_outside_lookback():
    current = _snap(0, 10.0, 1000.0, datetime(2026, 8, 10, 13, 30))
    # 30 minutes before current -- outside the default 20-minute lookback.
    bars = [_bar(50, 9.0, "2026-08-10T13:00:00.000+0000")]
    assert seed_history_from_bars(bars, current=current, lookback_minutes=20) == []


def test_seed_history_from_bars_excludes_bars_at_or_after_current():
    current = _snap(0, 10.0, 1000.0, datetime(2026, 8, 10, 13, 30))
    bars = [_bar(50, 9.0, "2026-08-10T13:30:00.000+0000")]  # exactly at current's own timestamp
    assert seed_history_from_bars(bars, current=current) == []


def test_seed_history_from_bars_sorts_chronologically():
    current = _snap(0, 10.0, 1000.0, datetime(2026, 8, 10, 13, 30))
    bars = [
        _bar(200, 9.8, "2026-08-10T13:25:00.000+0000"),
        _bar(100, 9.5, "2026-08-10T13:20:00.000+0000"),
    ]
    seeded = seed_history_from_bars(bars, current=current)
    assert [s.timestamp for s in seeded] == sorted(s.timestamp for s in seeded)
    assert seeded[0].last_price == 9.5
    assert seeded[1].last_price == 9.8


def test_seed_history_from_bars_anchors_cumulative_volume_to_current():
    # total_recent_volume = 100 + 200 = 300; running starts at 1000-300=700,
    # then walks forward bar by bar.
    current = _snap(0, 10.0, 1000.0, datetime(2026, 8, 10, 13, 30))
    bars = [
        _bar(100, 9.5, "2026-08-10T13:20:00.000+0000"),
        _bar(200, 9.8, "2026-08-10T13:25:00.000+0000"),
    ]
    seeded = seed_history_from_bars(bars, current=current)
    assert seeded[0].cumulative_volume == 800.0   # 700 + 100
    assert seeded[1].cumulative_volume == 1000.0  # 800 + 200
    # The critical invariant: the last seed snapshot must land exactly on
    # current's real cumulative volume, so the live feed that follows is
    # continuous with it -- no artificial jump/spike at the seam.
    assert seeded[-1].cumulative_volume == current.cumulative_volume


def test_seed_history_from_bars_clamps_running_volume_at_zero():
    # A single bar's volume alone exceeds current's total -- a data
    # inconsistency that must clamp to 0 rather than go negative.
    current = _snap(0, 10.0, 50.0, datetime(2026, 8, 10, 13, 30))
    bars = [_bar(1000, 9.5, "2026-08-10T13:25:00.000+0000")]
    seeded = seed_history_from_bars(bars, current=current)
    assert seeded[0].cumulative_volume == 1000.0  # max(0, 50-1000)=0, then +1000


def test_seeded_history_lets_compute_metrics_see_pre_discovery_momentum():
    # The actual bug this fixes: without seeding, a candidate discovered
    # right after a huge pre-discovery move shows 0 for every window-diffed
    # metric (volume/price acceleration, short-term RVOL) because its
    # rolling history starts as a single snapshot. With the same bars
    # BroadScanner already fetched spliced in ahead of that snapshot, the
    # window sees the real run-up instead.
    now = datetime(2026, 8, 10, 13, 30)
    current = _snap(0, 2.0, 1_000_000.0, now)
    bars = [
        _bar(400_000, 1.0, "2026-08-10T13:26:00.000+0000"),
        _bar(600_000, 1.5, "2026-08-10T13:28:00.000+0000"),
    ]
    seeded = seed_history_from_bars(bars, current=current)

    without_seed = compute_metrics(5_000_000, [current])
    with_seed = compute_metrics(5_000_000, seeded + [current])

    assert without_seed.volume_5m == 0.0
    assert without_seed.price_velocity_5m == 0.0
    assert with_seed.volume_5m == 600_000.0
    assert with_seed.price_velocity_5m == 100.0  # (2.0 - 1.0) / 1.0 * 100


# -- TICK-derived order flow (2026-08-14) -----------------------------------

def _tick(seconds_ago, price, volume, side, now):
    return TickRecord(symbol="TEST", timestamp=now - timedelta(seconds=seconds_ago), price=price, volume=volume, side=side)


def test_compute_metrics_without_ticks_leaves_order_flow_unset():
    now = datetime(2026, 1, 1, 10, 0, 0)
    m = compute_metrics(1_000_000, [_snap(0, 10.0, 100_000, now)])
    assert m.buy_volume_1m == 0.0
    assert m.sell_volume_1m == 0.0
    assert m.order_flow_imbalance_1m is None
    assert m.order_flow_sample_count_1m == 0
    assert m.trade_velocity is None


def test_compute_metrics_sums_classified_volume_within_the_1m_window():
    now = datetime(2026, 1, 1, 10, 0, 0)
    ticks = [
        _tick(10, 10.0, 80.0, TradeSide.BUY, now),
        _tick(5, 10.0, 20.0, TradeSide.SELL, now),
        _tick(2, 10.0, 30.0, TradeSide.UNKNOWN, now),  # counts toward neither side
        _tick(90, 10.0, 999.0, TradeSide.BUY, now),  # outside the 1m window -- excluded
    ]
    m = compute_metrics(1_000_000, [_snap(0, 10.0, 100_000, now)], ticks=ticks)
    assert m.buy_volume_1m == 80.0
    assert m.sell_volume_1m == 20.0
    assert m.order_flow_imbalance_1m == 0.6  # (80-20)/(80+20)
    assert m.order_flow_sample_count_1m == 2  # only the 2 classified prints
    assert m.trade_velocity == 3 / 60.0  # 3 total prints in-window (incl. UNKNOWN) / 60s


def test_compute_metrics_order_flow_imbalance_stays_none_when_all_ticks_unclassified():
    now = datetime(2026, 1, 1, 10, 0, 0)
    ticks = [_tick(5, 10.0, 100.0, TradeSide.UNKNOWN, now)]
    m = compute_metrics(1_000_000, [_snap(0, 10.0, 100_000, now)], ticks=ticks)
    assert m.buy_volume_1m == 0.0
    assert m.sell_volume_1m == 0.0
    assert m.order_flow_imbalance_1m is None  # nothing classified -- not "confirmed balanced"
    assert m.trade_velocity == 1 / 60.0  # the print still counts toward activity/trade_velocity
