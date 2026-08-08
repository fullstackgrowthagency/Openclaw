from webull_bot.metrics.calculations import (
    bid_ask_spread,
    dollar_volume,
    float_turnover,
    price_acceleration,
    price_velocity_pct,
    relative_volume,
    safe_div,
    volume_acceleration,
    vwap,
)


def test_safe_div_avoids_zero_division():
    assert safe_div(10, 0) == 0.0
    assert safe_div(10, 0, default=-1) == -1
    assert safe_div(10, 5) == 2.0


def test_float_turnover():
    assert float_turnover(1_000_000, 2_000_000) == 0.5


def test_relative_volume_defaults_to_one_when_unknown():
    assert relative_volume(500_000, 0) == 1.0
    assert relative_volume(1_000_000, 500_000) == 2.0


def test_volume_acceleration():
    assert volume_acceleration(200, 100) == 2.0
    assert volume_acceleration(0, 0) == 1.0


def test_price_velocity_and_acceleration():
    v_now = price_velocity_pct(11.0, 10.0)
    assert round(v_now, 4) == 10.0
    accel = price_acceleration(v_now, 2.0)
    assert round(accel, 4) == 8.0


def test_vwap():
    assert vwap(1000.0, 100.0) == 10.0


def test_bid_ask_spread():
    spread, spread_pct = bid_ask_spread(9.9, 10.1)
    assert round(spread, 2) == 0.2
    assert spread_pct > 0

    assert bid_ask_spread(0, 10) == (0.0, 0.0)
    assert bid_ask_spread(10, 9) == (0.0, 0.0)  # crossed/bad quote


def test_dollar_volume():
    assert dollar_volume(5.0, 100_000) == 500_000.0
