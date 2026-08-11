from datetime import datetime

from webull_bot.market_hours import (
    is_after_core_trading_hours,
    is_within_closing_buffer,
    is_within_core_trading_hours,
)


def test_within_core_hours_at_open():
    # 13:30 UTC = 9:30am ET in August (EDT, UTC-4) on a Monday.
    assert is_within_core_trading_hours(datetime(2026, 8, 10, 13, 30, 0)) is True


def test_within_core_hours_just_before_close():
    # 19:59 UTC = 3:59pm ET.
    assert is_within_core_trading_hours(datetime(2026, 8, 10, 19, 59, 0)) is True


def test_outside_core_hours_before_open():
    # 13:29 UTC = 9:29am ET, one minute before open.
    assert is_within_core_trading_hours(datetime(2026, 8, 10, 13, 29, 0)) is False


def test_outside_core_hours_at_close_boundary():
    # 20:00 UTC = 4:00pm ET exactly -- close boundary is exclusive.
    assert is_within_core_trading_hours(datetime(2026, 8, 10, 20, 0, 0)) is False


def test_outside_core_hours_on_weekend():
    saturday = datetime(2026, 8, 15, 15, 0, 0)  # 11:00am ET, a Saturday
    assert is_within_core_trading_hours(saturday) is False


def test_after_core_hours_is_false_before_open():
    # Before the open isn't "after hours" -- it's "not yet open" -- and
    # these are deliberately different predicates (see market_hours.py's
    # docstring): the end-of-day auto-flatten must not fire pre-market.
    pre_market = datetime(2026, 8, 10, 12, 0, 0)  # 8:00am ET
    assert is_after_core_trading_hours(pre_market) is False


def test_after_core_hours_is_true_at_and_after_close():
    at_close = datetime(2026, 8, 10, 20, 0, 0)  # 4:00pm ET exactly
    assert is_after_core_trading_hours(at_close) is True
    well_after_close = datetime(2026, 8, 10, 23, 0, 0)  # 7:00pm ET
    assert is_after_core_trading_hours(well_after_close) is True


def test_after_core_hours_is_true_on_weekend():
    saturday = datetime(2026, 8, 15, 15, 0, 0)
    assert is_after_core_trading_hours(saturday) is True


def test_within_closing_buffer_is_false_well_before_the_buffer_window():
    # 19:50 UTC = 3:50pm ET -- 10 minutes before close, outside a 2-minute buffer.
    assert is_within_closing_buffer(datetime(2026, 8, 10, 19, 50, 0), buffer_minutes=2.0) is False


def test_within_closing_buffer_is_true_right_at_the_buffer_start():
    # 19:58 UTC = 3:58pm ET exactly -- the buffer boundary itself is inclusive.
    assert is_within_closing_buffer(datetime(2026, 8, 10, 19, 58, 0), buffer_minutes=2.0) is True


def test_within_closing_buffer_is_true_one_second_into_the_buffer():
    assert is_within_closing_buffer(datetime(2026, 8, 10, 19, 58, 1), buffer_minutes=2.0) is True


def test_within_closing_buffer_stays_true_after_the_actual_close():
    # Not a one-shot window -- must keep firing every tick after 4:00pm too,
    # so a position that somehow didn't flatten in the buffer window (or
    # opened in the last seconds before close) still gets caught.
    well_after_close = datetime(2026, 8, 10, 23, 0, 0)  # 7:00pm ET
    assert is_within_closing_buffer(well_after_close, buffer_minutes=2.0) is True


def test_within_closing_buffer_is_true_all_weekend():
    saturday = datetime(2026, 8, 15, 15, 0, 0)
    assert is_within_closing_buffer(saturday, buffer_minutes=2.0) is True
