"""
Hermetic tests for YFinanceFloatProvider -- no real network calls (and no
dependency on the `yfinance` package actually being importable, since the
real fetcher is injected out entirely). See yfinance_provider.py's module
docstring for why this is a fallback-only provider, never primary.
"""
import pytest

from webull_bot.data.float_providers.yfinance_provider import YFinanceFloatProvider


def test_get_float_data_maps_fields_correctly():
    provider = YFinanceFloatProvider(info_fetcher=lambda symbol: {
        "floatShares": 409_140_234,
        "sharesOutstanding": 448_691_257,
        "marketCap": 12_000_000_000,
    })

    data = provider.get_float_data("GME")

    assert data.symbol == "GME"
    assert data.free_float_shares == 409_140_234
    assert data.shares_outstanding == 448_691_257
    assert data.market_cap == 12_000_000_000
    assert round(data.float_percent, 4) == round(409_140_234 / 448_691_257, 4)
    assert data.effective_date is None
    assert data.source == "yfinance"


def test_get_float_data_raises_when_float_shares_missing():
    provider = YFinanceFloatProvider(info_fetcher=lambda symbol: {"sharesOutstanding": 1_000_000})
    with pytest.raises(ValueError):
        provider.get_float_data("NOFLOAT")


def test_get_float_data_survives_missing_shares_outstanding():
    # Yahoo's payload is occasionally partially populated -- a missing
    # sharesOutstanding must not block an otherwise-valid floatShares read.
    provider = YFinanceFloatProvider(info_fetcher=lambda symbol: {"floatShares": 1_000_000})
    data = provider.get_float_data("PARTIAL")
    assert data.free_float_shares == 1_000_000
    assert data.shares_outstanding == 1_000_000
    assert data.float_percent == 1.0


def test_get_float_data_survives_missing_market_cap():
    provider = YFinanceFloatProvider(info_fetcher=lambda symbol: {"floatShares": 500_000, "sharesOutstanding": 1_000_000})
    data = provider.get_float_data("NOMKTCAP")
    assert data.market_cap is None


def test_get_float_data_bulk_skips_failures():
    def fetcher(symbol):
        if symbol == "MISSING":
            raise RuntimeError("simulated Yahoo failure")
        return {"floatShares": 1_000_000, "sharesOutstanding": 2_000_000}

    provider = YFinanceFloatProvider(info_fetcher=fetcher)
    result = provider.get_float_data_bulk(["OK", "MISSING"])
    assert "OK" in result
    assert "MISSING" not in result
