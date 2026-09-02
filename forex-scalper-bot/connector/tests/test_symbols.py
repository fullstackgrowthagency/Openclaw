import pytest

from fx_connector.symbols import mt5_symbol_to_wire_pair, wire_pair_to_mt5_symbol


def test_wire_pair_to_mt5_symbol_strips_slash():
    assert wire_pair_to_mt5_symbol("EUR/USD") == "EURUSD"


def test_wire_pair_to_mt5_symbol_appends_suffix():
    assert wire_pair_to_mt5_symbol("EUR/USD", suffix=".m") == "EURUSD.m"


def test_wire_pair_to_mt5_symbol_rejects_missing_slash():
    with pytest.raises(ValueError):
        wire_pair_to_mt5_symbol("EURUSD")


def test_mt5_symbol_to_wire_pair_strips_suffix_first():
    assert mt5_symbol_to_wire_pair("EURUSD.m", suffix=".m") == "EUR/USD"


def test_mt5_symbol_to_wire_pair_round_trips_with_no_suffix():
    assert mt5_symbol_to_wire_pair(wire_pair_to_mt5_symbol("GBP/JPY")) == "GBP/JPY"


def test_mt5_symbol_to_wire_pair_rejects_non_six_char_symbol():
    with pytest.raises(ValueError):
        mt5_symbol_to_wire_pair("US30")  # an index, not a 6-char currency pair


def test_mt5_symbol_to_wire_pair_rejects_symbol_missing_expected_suffix():
    with pytest.raises(ValueError):
        mt5_symbol_to_wire_pair("EURUSD", suffix=".m")
