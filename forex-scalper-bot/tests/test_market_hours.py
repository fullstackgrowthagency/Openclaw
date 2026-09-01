from datetime import datetime

import pytest

from fx_bot.market_hours import (
    active_sessions,
    is_market_open,
    is_within_london_new_york_overlap,
    seconds_until_market_open,
)

# Wednesday 2026-09-02 15:00 UTC -- inside the London/New York overlap.
_MID_WEEK_OVERLAP = datetime(2026, 9, 2, 15, 0, 0)


def test_market_open_monday_through_thursday_all_day():
    monday_midnight = datetime(2026, 8, 31, 0, 0, 0)
    thursday_late = datetime(2026, 9, 3, 23, 59, 0)
    assert is_market_open(monday_midnight)
    assert is_market_open(thursday_late)


def test_market_closed_saturday_all_day():
    saturday = datetime(2026, 9, 5, 12, 0, 0)
    assert not is_market_open(saturday)


def test_market_closed_sunday_before_2200_utc_open_after():
    sunday_morning = datetime(2026, 9, 6, 10, 0, 0)
    sunday_evening = datetime(2026, 9, 6, 23, 0, 0)
    assert not is_market_open(sunday_morning)
    assert is_market_open(sunday_evening)


def test_market_closed_friday_after_2200_utc_open_before():
    friday_afternoon = datetime(2026, 9, 4, 15, 0, 0)
    friday_night = datetime(2026, 9, 4, 23, 0, 0)
    assert is_market_open(friday_afternoon)
    assert not is_market_open(friday_night)


def test_active_sessions_includes_london_and_new_york_during_overlap():
    sessions = active_sessions(_MID_WEEK_OVERLAP)
    assert "london" in sessions
    assert "new_york" in sessions


def test_active_sessions_empty_when_market_closed():
    assert active_sessions(datetime(2026, 9, 5, 12, 0, 0)) == set()


def test_is_within_london_new_york_overlap_true_at_1500_utc():
    assert is_within_london_new_york_overlap(_MID_WEEK_OVERLAP)


def test_is_within_london_new_york_overlap_false_outside_the_band():
    tokyo_only = datetime(2026, 9, 2, 2, 0, 0)
    assert not is_within_london_new_york_overlap(tokyo_only)


def test_seconds_until_market_open_is_zero_while_open():
    assert seconds_until_market_open(_MID_WEEK_OVERLAP) == 0.0


def test_seconds_until_market_open_counts_down_to_next_sunday_2200_utc():
    saturday_noon = datetime(2026, 9, 5, 12, 0, 0)
    seconds = seconds_until_market_open(saturday_noon)
    expected_open = datetime(2026, 9, 6, 22, 0, 0)
    assert seconds == pytest.approx((expected_open - saturday_noon).total_seconds())
