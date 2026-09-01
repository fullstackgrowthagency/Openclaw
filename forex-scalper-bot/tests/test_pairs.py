import pytest

from fx_bot.pairs import base_quote, pip_size, pips_to_price_diff, price_diff_to_pips


def test_base_quote_splits_correctly():
    assert base_quote("EUR/USD") == ("EUR", "USD")
    assert base_quote("usd/jpy") == ("USD", "JPY")


def test_base_quote_rejects_malformed_pair():
    with pytest.raises(ValueError):
        base_quote("EURUSD")
    with pytest.raises(ValueError):
        base_quote("EUR/")


def test_pip_size_is_point_zero_one_for_jpy_pairs():
    assert pip_size("USD/JPY") == 0.01
    assert pip_size("EUR/JPY") == 0.01


def test_pip_size_is_point_zero_zero_zero_one_for_other_pairs():
    assert pip_size("EUR/USD") == 0.0001
    assert pip_size("GBP/USD") == 0.0001


def test_price_diff_to_pips_round_trips_with_pips_to_price_diff():
    pair = "EUR/USD"
    pips = price_diff_to_pips(pair, 0.0025)
    assert pips == pytest.approx(25.0)
    assert pips_to_price_diff(pair, pips) == pytest.approx(0.0025)
