from datetime import datetime, timedelta

from webull_bot.metrics.volume_profile import (
    VolumeNode,
    compute_volume_profile,
    filter_bars_by_lookback,
    high_volume_node_levels,
)


def _bar(low, high, volume, time="2026-08-01T12:00:00.000+0000"):
    return {"time": time, "low": str(low), "high": str(high), "volume": str(volume)}


def test_compute_volume_profile_empty_input_returns_no_nodes():
    assert compute_volume_profile([]) == []


def test_compute_volume_profile_single_bar_goes_into_touched_buckets():
    # A bar spanning the full [0, 10] range with num_buckets=10 touches
    # every bucket; its volume should be split evenly across all of them.
    bars = [_bar(0, 10, 1000)]
    nodes = compute_volume_profile(bars, num_buckets=10)
    assert len(nodes) == 10
    assert sum(n.volume for n in nodes) == 1000
    assert all(n.volume == 100 for n in nodes)


def test_compute_volume_profile_concentrates_volume_where_bars_overlap():
    # Two bars both touching [4, 6] (out of a [0, 10] overall range) should
    # produce a clear volume peak there relative to bars that only touch
    # their own narrow range once.
    bars = [
        _bar(0, 10, 100),   # spread thin across everything
        _bar(4, 6, 5000),   # concentrated in the middle
        _bar(4, 6, 5000),
    ]
    nodes = compute_volume_profile(bars, num_buckets=10)
    peak = max(nodes, key=lambda n: n.volume)
    assert 4 <= peak.price <= 6


def test_compute_volume_profile_zero_range_bar_goes_into_one_bucket():
    bars = [_bar(5, 5, 300)]
    nodes = compute_volume_profile(bars, num_buckets=10)
    assert len(nodes) == 1
    assert nodes[0].volume == 300


def test_compute_volume_profile_all_bars_same_price_returns_single_node():
    bars = [_bar(5, 5, 100), _bar(5, 5, 200)]
    nodes = compute_volume_profile(bars, num_buckets=50)
    assert nodes == [VolumeNode(price=5.0, volume=300)]


def test_compute_volume_profile_zero_volume_bars_produce_no_nodes():
    bars = [_bar(1, 2, 0)]
    assert compute_volume_profile(bars, num_buckets=10) == []


def test_high_volume_node_levels_excludes_noise_below_threshold():
    nodes = [
        VolumeNode(price=1.0, volume=1000),  # the max
        VolumeNode(price=2.0, volume=400),   # 40% of max -- above default 30% threshold
        VolumeNode(price=3.0, volume=100),   # 10% of max -- below threshold, excluded
    ]
    levels = high_volume_node_levels(nodes, top_n=5, min_volume_pct_of_max=0.3)
    assert sorted(levels) == [1.0, 2.0]


def test_high_volume_node_levels_respects_top_n():
    nodes = [VolumeNode(price=float(i), volume=float(100 - i)) for i in range(10)]
    levels = high_volume_node_levels(nodes, top_n=3, min_volume_pct_of_max=0.0)
    assert len(levels) == 3
    assert sorted(levels) == [0.0, 1.0, 2.0]  # lowest i has highest volume (100 - i)


def test_high_volume_node_levels_empty_input():
    assert high_volume_node_levels([]) == []


def test_filter_bars_by_lookback_drops_bars_older_than_cutoff():
    now = datetime(2026, 8, 9, 12, 0, 0)
    bars = [
        _bar(1, 2, 100, time="2026-08-09T11:00:00.000+0000"),  # 1 hour ago -- kept
        _bar(1, 2, 100, time="2026-06-01T11:00:00.000+0000"),  # months ago -- dropped
    ]
    kept = filter_bars_by_lookback(bars, lookback_days=20, now=now)
    assert len(kept) == 1
    assert kept[0]["time"] == "2026-08-09T11:00:00.000+0000"


def test_filter_bars_by_lookback_keeps_bar_exactly_at_cutoff():
    now = datetime(2026, 8, 9, 12, 0, 0)
    cutoff_time = now - timedelta(days=20)
    bars = [_bar(1, 2, 100, time=cutoff_time.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+0000")]
    kept = filter_bars_by_lookback(bars, lookback_days=20, now=now)
    assert len(kept) == 1
