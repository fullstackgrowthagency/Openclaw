from datetime import datetime

from webull_bot.metrics.volume_baseline import (
    VolumeBaseline,
    _phase_and_bucket,
    compute_volume_baseline,
)


def _bar(volume, time):
    """`time` is a UTC ISO string (Webull's raw bar shape) -- tests below
    pick UTC times that land on round US/Eastern clock times during EDT
    (UTC-4), e.g. 13:30 UTC == 9:30am ET, matching this project's other
    fixtures (test_volume_profile.py, test_webull_broker_client.py)."""
    return {"time": time, "volume": str(volume)}


# -- _phase_and_bucket boundaries --------------------------------------------

def test_phase_and_bucket_classifies_premarket():
    # 8:00am ET == 12:00 UTC (EDT, UTC-4)
    assert _phase_and_bucket(datetime(2026, 8, 3, 12, 0), 5) == ("PRE", 240)


def test_phase_and_bucket_classifies_regular_session_open():
    # 9:30am ET == 13:30 UTC -- the open itself, bucket 0 of RTH.
    assert _phase_and_bucket(datetime(2026, 8, 3, 13, 30), 5) == ("RTH", 0)


def test_phase_and_bucket_classifies_regular_session_mid_bucket():
    # 9:33am ET -- 3 minutes into RTH, rounds down to bucket 0 (5-min buckets).
    assert _phase_and_bucket(datetime(2026, 8, 3, 13, 33), 5) == ("RTH", 0)
    # 9:37am ET -- 7 minutes in, rounds down to bucket 5.
    assert _phase_and_bucket(datetime(2026, 8, 3, 13, 37), 5) == ("RTH", 5)


def test_phase_and_bucket_classifies_after_hours():
    # 4:00pm ET == 20:00 UTC -- the close itself, bucket 0 of ATH.
    assert _phase_and_bucket(datetime(2026, 8, 3, 20, 0), 5) == ("ATH", 0)


def test_phase_and_bucket_returns_none_overnight():
    # 2:00am ET == 06:00 UTC -- before PRE even starts (4:00am ET).
    assert _phase_and_bucket(datetime(2026, 8, 3, 6, 0), 5) is None
    # 8:30pm ET == 00:30 UTC (next day) -- after ATH ends (8:00pm ET).
    assert _phase_and_bucket(datetime(2026, 8, 4, 0, 30), 5) is None


# -- compute_volume_baseline / VolumeBaseline.lookup -------------------------

def test_compute_volume_baseline_empty_bars_returns_empty_baseline():
    baseline = compute_volume_baseline([])
    assert baseline.typical_cumulative == {}
    assert baseline.lookup(datetime(2026, 8, 5, 13, 30)) == (None, None, None)


def test_compute_volume_baseline_excludes_today():
    now = datetime(2026, 8, 5, 14, 0)  # "today" is 2026-08-05
    bars = [
        _bar(100, "2026-08-05T13:30:00.000+0000"),  # today -- must be excluded
    ]
    baseline = compute_volume_baseline(bars, now=now)
    assert baseline.typical_cumulative == {}


def test_compute_volume_baseline_averages_across_historical_days():
    now = datetime(2026, 8, 5, 14, 0)
    bars = [
        # RTH bucket 0 (9:30-9:35am ET) on two different historical days.
        _bar(100, "2026-08-03T13:30:00.000+0000"),
        _bar(300, "2026-08-04T13:30:00.000+0000"),
    ]
    baseline = compute_volume_baseline(bars, now=now)
    typical_same_time, typical_1m, typical_5m = baseline.lookup(datetime(2026, 8, 5, 13, 30))
    assert typical_same_time == 200.0  # average cumulative at bucket 0: (100+300)/2
    assert typical_5m == 200.0         # average volume IN bucket 0
    assert typical_1m == 40.0          # 200 / bucket_minutes(5), the uniform-rate approximation


def test_compute_volume_baseline_cumulative_sums_within_a_day():
    now = datetime(2026, 8, 5, 14, 0)
    bars = [
        _bar(100, "2026-08-03T13:30:00.000+0000"),  # RTH bucket 0
        _bar(20, "2026-08-03T13:35:00.000+0000"),   # RTH bucket 5
    ]
    baseline = compute_volume_baseline(bars, now=now)
    _, _, bucket0_volume = baseline.lookup(datetime(2026, 8, 5, 13, 30))
    same_time_5, _, bucket5_volume = baseline.lookup(datetime(2026, 8, 5, 13, 35))
    assert bucket0_volume == 100.0
    assert bucket5_volume == 20.0
    assert same_time_5 == 120.0  # cumulative: bucket 0 (100) + bucket 5 (20)


def test_compute_volume_baseline_keeps_phases_independently_reset():
    now = datetime(2026, 8, 5, 14, 0)
    bars = [
        _bar(50, "2026-08-03T12:00:00.000+0000"),   # PRE, 8:00am ET
        _bar(100, "2026-08-03T13:30:00.000+0000"),  # RTH, 9:30am ET (bucket 0)
    ]
    baseline = compute_volume_baseline(bars, now=now)
    typical_same_time, _, _ = baseline.lookup(datetime(2026, 8, 5, 13, 30))
    # RTH bucket 0's cumulative must be its own 100, not 150 (PRE's volume
    # must not leak into RTH's independently-reset cumulative curve).
    assert typical_same_time == 100.0


def test_compute_volume_baseline_lookup_none_for_bucket_with_no_history():
    now = datetime(2026, 8, 5, 14, 0)
    bars = [_bar(100, "2026-08-03T13:30:00.000+0000")]  # only bucket 0 has data
    baseline = compute_volume_baseline(bars, now=now)
    # 10:00am ET -- a later bucket no historical day ever reached.
    assert baseline.lookup(datetime(2026, 8, 5, 14, 0)) == (None, None, None)


def test_compute_volume_baseline_lookup_none_outside_all_phases():
    now = datetime(2026, 8, 5, 14, 0)
    bars = [_bar(100, "2026-08-03T13:30:00.000+0000")]
    baseline = compute_volume_baseline(bars, now=now)
    assert baseline.lookup(datetime(2026, 8, 5, 6, 0)) == (None, None, None)  # 2am ET


def test_volume_baseline_is_frozen_dataclass_with_defaults():
    baseline = VolumeBaseline(bucket_minutes=5)
    assert baseline.typical_cumulative == {}
    assert baseline.typical_bucket_volume == {}
