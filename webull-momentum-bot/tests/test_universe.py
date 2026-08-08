"""
Hermetic tests for data/universe.py -- no real network calls or Webull SDK
import required at test time for the fake-data_client tests (the Webull
provider classes only `import webull.data.common.category` lazily inside
get_symbols(), and the real SDK is installed in this project's venv anyway).
"""
from dataclasses import dataclass, field

import pytest

from webull_bot.data.universe import (
    MultiSourceUniverseProvider,
    StaticUniverseProvider,
    WebullGainersLosersConfig,
    WebullGainersLosersUniverseProvider,
    WebullUniverseConfig,
    WebullUniverseProvider,
)


@dataclass
class _FakeResponse:
    _payload: dict
    status_code: int = 200

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


@dataclass
class _FakeScreener:
    most_active_rows: list = field(default_factory=list)
    gainers_losers_rows: list = field(default_factory=list)
    calls: list = field(default_factory=list)

    def get_most_active(self, category, rank_type=None, sort_by=None, page_size=None, **kwargs):
        self.calls.append(("get_most_active", rank_type))
        return _FakeResponse({"data": self.most_active_rows})

    def get_gainers_losers(self, rank_type=None, category=None, sort_by=None, direction=None, page_size=None, **kwargs):
        self.calls.append(("get_gainers_losers", rank_type))
        return _FakeResponse({"data": self.gainers_losers_rows})


class _FakeDataClient:
    def __init__(self, screener: _FakeScreener):
        self.screener = screener


def _row(symbol, price, market_value=1_000_000):
    return {"symbol": symbol, "price": str(price), "market_value": str(market_value)}


# -- WebullUniverseProvider (get_most_active) -------------------------------

def test_webull_universe_provider_filters_by_price_range():
    screener = _FakeScreener(most_active_rows=[_row("A", 0.50), _row("B", 5.0), _row("C", 25.0)])
    provider = WebullUniverseProvider(_FakeDataClient(screener))
    assert provider.get_symbols() == ["B"]


def test_webull_universe_provider_filters_by_market_value():
    screener = _FakeScreener(most_active_rows=[_row("SMALL", 5.0, 1_000_000), _row("HUGE", 5.0, 5_000_000_000)])
    provider = WebullUniverseProvider(_FakeDataClient(screener), WebullUniverseConfig(max_market_value=2_000_000_000))
    assert provider.get_symbols() == ["SMALL"]


def test_webull_universe_provider_uses_configured_rank_type():
    screener = _FakeScreener(most_active_rows=[])
    provider = WebullUniverseProvider(_FakeDataClient(screener), WebullUniverseConfig(rank_type="TURNOVER_RATE"))
    provider.get_symbols()
    assert screener.calls == [("get_most_active", "TURNOVER_RATE")]


def test_webull_universe_provider_skips_rows_with_bad_price():
    screener = _FakeScreener(most_active_rows=[{"symbol": "BAD"}, _row("OK", 5.0)])
    provider = WebullUniverseProvider(_FakeDataClient(screener))
    assert provider.get_symbols() == ["OK"]


# -- WebullGainersLosersUniverseProvider ------------------------------------

def test_gainers_losers_provider_filters_same_as_most_active():
    screener = _FakeScreener(gainers_losers_rows=[_row("A", 0.50), _row("B", 5.0)])
    provider = WebullGainersLosersUniverseProvider(_FakeDataClient(screener))
    assert provider.get_symbols() == ["B"]


def test_gainers_losers_provider_uses_configured_rank_type_and_direction():
    screener = _FakeScreener(gainers_losers_rows=[])
    provider = WebullGainersLosersUniverseProvider(
        _FakeDataClient(screener), WebullGainersLosersConfig(rank_type="DAY_5", direction="ASC")
    )
    provider.get_symbols()
    assert screener.calls == [("get_gainers_losers", "DAY_5")]


# -- MultiSourceUniverseProvider ---------------------------------------------

class _ListProvider:
    def __init__(self, symbols):
        self._symbols = symbols

    def get_symbols(self):
        return list(self._symbols)


class _FailingProvider:
    def get_symbols(self):
        raise RuntimeError("simulated failure")


def test_multi_source_dedupes_across_sources():
    provider = MultiSourceUniverseProvider([_ListProvider(["A", "B"]), _ListProvider(["B", "C"])])
    symbols = provider.get_symbols()
    assert sorted(symbols) == ["A", "B", "C"]
    assert len(symbols) == len(set(symbols))


def test_multi_source_interleaves_round_robin_not_source_order():
    """A symbol from source 2 should appear before exhausting source 1's
    full list -- this is what prevents downstream truncation from favoring
    whichever source happens to be listed first."""
    provider = MultiSourceUniverseProvider([
        _ListProvider(["A1", "A2", "A3", "A4"]),
        _ListProvider(["B1", "B2"]),
    ])
    symbols = provider.get_symbols()
    # Round-robin: A1, B1, A2, B2, A3, A4
    assert symbols == ["A1", "B1", "A2", "B2", "A3", "A4"]


def test_multi_source_isolates_failing_source():
    provider = MultiSourceUniverseProvider([_FailingProvider(), _ListProvider(["A", "B"])])
    assert provider.get_symbols() == ["A", "B"]


def test_multi_source_all_sources_failing_returns_empty():
    provider = MultiSourceUniverseProvider([_FailingProvider(), _FailingProvider()])
    assert provider.get_symbols() == []


def test_multi_source_with_no_providers_returns_empty():
    assert MultiSourceUniverseProvider([]).get_symbols() == []


# -- StaticUniverseProvider ---------------------------------------------------

def test_static_universe_provider_returns_fixed_list():
    provider = StaticUniverseProvider(["AAPL", "GME"])
    assert provider.get_symbols() == ["AAPL", "GME"]
