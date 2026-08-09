"""
Hermetic tests for data/universe.py -- no real network calls or Webull SDK
import required at test time for the fake-data_client tests (the Webull
provider classes only `import webull.data.common.category` lazily inside
get_symbols(), and the real SDK is installed in this project's venv anyway).
"""
from dataclasses import dataclass, field

import pytest

from webull_bot.brokers.webull.retry import webull_market_data_limiter
from webull_bot.data.universe import (
    MultiSourceUniverseProvider,
    StaticUniverseProvider,
    WebullGainersLosersConfig,
    WebullGainersLosersUniverseProvider,
    WebullUniverseConfig,
    WebullUniverseProvider,
)


@pytest.fixture(autouse=True)
def _no_rate_limit_delay(monkeypatch):
    """get_symbols() calls call_with_retry, which paces every attempt
    through the real, process-wide webull_market_data_limiter regardless
    of whether the underlying screener call is real or faked -- without
    this, these hermetic tests get throttled by a safety mechanism meant
    for live API pacing, not fake in-memory calls (confirmed: this file
    alone went from ~5s to ~24s once pagination tests added more calls per
    test). Neutralize it for this module only; production behavior is
    untouched since this only patches the shared singleton's interval for
    the duration of each test."""
    monkeypatch.setattr(webull_market_data_limiter, "min_interval_seconds", 0.0)


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
    """most_active_pages/gainers_losers_pages: list of pages, each page a
    list of raw row dicts -- simulates real pagination (has_more derived
    from whether another page exists), rather than a single flat list."""
    most_active_pages: list = field(default_factory=list)
    gainers_losers_pages: list = field(default_factory=list)
    calls: list = field(default_factory=list)

    def get_most_active(self, category, rank_type=None, sort_by=None, page_index=None, page_size=None, **kwargs):
        self.calls.append(("get_most_active", rank_type, page_index))
        idx = int(page_index or 1) - 1
        rows = self.most_active_pages[idx] if 0 <= idx < len(self.most_active_pages) else []
        has_more = idx + 1 < len(self.most_active_pages)
        return _FakeResponse({"data": rows, "has_more": has_more})

    def get_gainers_losers(self, rank_type=None, category=None, sort_by=None, direction=None, page_index=None, page_size=None, **kwargs):
        self.calls.append(("get_gainers_losers", rank_type, page_index))
        idx = int(page_index or 1) - 1
        rows = self.gainers_losers_pages[idx] if 0 <= idx < len(self.gainers_losers_pages) else []
        has_more = idx + 1 < len(self.gainers_losers_pages)
        return _FakeResponse({"data": rows, "has_more": has_more})


def _one_page_screener(rows=None, gl_rows=None) -> _FakeScreener:
    """Convenience for tests that don't care about pagination -- a single
    page with has_more=False."""
    return _FakeScreener(
        most_active_pages=[rows] if rows is not None else [],
        gainers_losers_pages=[gl_rows] if gl_rows is not None else [],
    )


class _FakeDataClient:
    def __init__(self, screener: _FakeScreener):
        self.screener = screener


def _row(symbol, price, market_value=1_000_000, **extra):
    return {"symbol": symbol, "price": str(price), "market_value": str(market_value), **extra}


# -- WebullUniverseProvider (get_most_active) -------------------------------

def test_webull_universe_provider_accepts_lower_price_boundary():
    screener = _one_page_screener(rows=[_row("A", 0.40)])
    provider = WebullUniverseProvider(_FakeDataClient(screener))
    assert provider.get_symbols() == ["A"]


def test_webull_universe_provider_accepts_upper_price_boundary():
    screener = _one_page_screener(rows=[_row("A", 25.00)])
    provider = WebullUniverseProvider(_FakeDataClient(screener))
    assert provider.get_symbols() == ["A"]


def test_webull_universe_provider_rejects_below_lower_price_boundary():
    screener = _one_page_screener(rows=[_row("A", 0.39)])
    provider = WebullUniverseProvider(_FakeDataClient(screener))
    assert provider.get_symbols() == []


def test_webull_universe_provider_rejects_above_upper_price_boundary():
    screener = _one_page_screener(rows=[_row("A", 25.01)])
    provider = WebullUniverseProvider(_FakeDataClient(screener))
    assert provider.get_symbols() == []


def test_webull_universe_provider_filters_by_price_range():
    screener = _one_page_screener(rows=[_row("A", 0.10), _row("B", 5.0), _row("C", 30.0)])
    provider = WebullUniverseProvider(_FakeDataClient(screener))
    assert provider.get_symbols() == ["B"]


def test_webull_universe_provider_filters_by_market_value():
    screener = _one_page_screener(rows=[_row("SMALL", 5.0, 1_000_000), _row("HUGE", 5.0, 5_000_000_000)])
    provider = WebullUniverseProvider(_FakeDataClient(screener), WebullUniverseConfig(max_market_value=2_000_000_000))
    assert provider.get_symbols() == ["SMALL"]


def test_webull_universe_provider_uses_configured_rank_type():
    screener = _one_page_screener(rows=[])
    provider = WebullUniverseProvider(_FakeDataClient(screener), WebullUniverseConfig(rank_type="TURNOVER_RATE"))
    provider.get_symbols()
    assert screener.calls == [("get_most_active", "TURNOVER_RATE", "1")]


def test_webull_universe_provider_skips_rows_with_bad_price():
    screener = _one_page_screener(rows=[{"symbol": "BAD"}, _row("OK", 5.0)])
    provider = WebullUniverseProvider(_FakeDataClient(screener))
    assert provider.get_symbols() == ["OK"]


# -- Pagination ---------------------------------------------------------------

def test_webull_universe_provider_follows_pagination_across_pages():
    screener = _FakeScreener(most_active_pages=[
        [_row("A", 5.0)],
        [_row("B", 5.0)],
        [_row("C", 5.0)],
    ])
    provider = WebullUniverseProvider(_FakeDataClient(screener), WebullUniverseConfig(max_pages=10))
    assert provider.get_symbols() == ["A", "B", "C"]
    assert [call[2] for call in screener.calls] == ["1", "2", "3"]


def test_webull_universe_provider_stops_when_a_page_is_empty():
    screener = _FakeScreener(most_active_pages=[[_row("A", 5.0)], []])
    # has_more would be False here anyway (last real page), but this also
    # covers a defensive stop if a page ever comes back empty regardless.
    provider = WebullUniverseProvider(_FakeDataClient(screener), WebullUniverseConfig(max_pages=10))
    assert provider.get_symbols() == ["A"]


def test_webull_universe_provider_stops_at_rank_value_threshold():
    screener = _FakeScreener(most_active_pages=[
        [_row("A", 5.0, relative_volume_10d="3.0")],
        [_row("B", 5.0, relative_volume_10d="1.5")],  # below threshold -- this page's symbols are still kept...
        [_row("C", 5.0, relative_volume_10d="1.0")],  # ...but pagination must not reach this page
    ])
    provider = WebullUniverseProvider(
        _FakeDataClient(screener),
        WebullUniverseConfig(max_pages=10, rank_value_field="relative_volume_10d", min_rank_value=2.0),
    )
    symbols = provider.get_symbols()
    assert symbols == ["A", "B"]
    assert [call[2] for call in screener.calls] == ["1", "2"]  # never fetched page 3


def test_webull_universe_provider_respects_max_pages_safety_valve():
    # has_more always True and no rank threshold set -- max_pages is the
    # only thing that stops this from paginating forever.
    class _NeverEndingScreener(_FakeScreener):
        def get_most_active(self, category, rank_type=None, sort_by=None, page_index=None, page_size=None, **kwargs):
            self.calls.append(("get_most_active", rank_type, page_index))
            return _FakeResponse({"data": [_row(f"SYM{page_index}", 5.0)], "has_more": True})

    screener = _NeverEndingScreener()
    provider = WebullUniverseProvider(_FakeDataClient(screener), WebullUniverseConfig(max_pages=3))
    symbols = provider.get_symbols()
    assert len(symbols) == 3
    assert len(screener.calls) == 3


# -- WebullGainersLosersUniverseProvider ------------------------------------

def test_gainers_losers_provider_accepts_lower_and_upper_price_boundaries():
    screener = _one_page_screener(gl_rows=[_row("A", 0.40), _row("B", 25.00)])
    provider = WebullGainersLosersUniverseProvider(_FakeDataClient(screener))
    assert sorted(provider.get_symbols()) == ["A", "B"]


def test_gainers_losers_provider_filters_same_as_most_active():
    screener = _one_page_screener(gl_rows=[_row("A", 0.10), _row("B", 5.0), _row("C", 30.0)])
    provider = WebullGainersLosersUniverseProvider(_FakeDataClient(screener))
    assert provider.get_symbols() == ["B"]


def test_gainers_losers_provider_uses_configured_rank_type_and_direction():
    screener = _one_page_screener(gl_rows=[])
    provider = WebullGainersLosersUniverseProvider(
        _FakeDataClient(screener), WebullGainersLosersConfig(rank_type="DAY_5", direction="ASC")
    )
    provider.get_symbols()
    assert screener.calls == [("get_gainers_losers", "DAY_5", "1")]


def test_gainers_losers_provider_supports_min_5_rank_type():
    # MIN_5 is the 4th discovery source's rank_type -- just a config value
    # from this provider's point of view, verified live to be a real,
    # distinct ranking (see WebullGainersLosersUniverseProvider's docstring).
    screener = _one_page_screener(gl_rows=[_row("FASTMOVER", 5.0)])
    provider = WebullGainersLosersUniverseProvider(
        _FakeDataClient(screener), WebullGainersLosersConfig(rank_type="MIN_5", sort_by="CHANGE_RATIO", direction="DESC")
    )
    assert provider.get_symbols() == ["FASTMOVER"]
    assert screener.calls == [("get_gainers_losers", "MIN_5", "1")]


def test_gainers_losers_provider_follows_pagination_across_pages():
    screener = _FakeScreener(gainers_losers_pages=[[_row("A", 5.0)], [_row("B", 5.0)]])
    provider = WebullGainersLosersUniverseProvider(_FakeDataClient(screener), WebullGainersLosersConfig(max_pages=10))
    assert provider.get_symbols() == ["A", "B"]


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


def test_multi_source_preserves_source_order_with_no_truncation_downstream():
    """Nothing downstream truncates this list anymore, so source order no
    longer needs interleaving to protect against one source dominating --
    plain source-by-source order (deduped) is fine since every symbol gets
    scanned regardless of position."""
    provider = MultiSourceUniverseProvider([
        _ListProvider(["A1", "A2", "A3", "A4"]),
        _ListProvider(["B1", "B2"]),
    ])
    symbols = provider.get_symbols()
    assert symbols == ["A1", "A2", "A3", "A4", "B1", "B2"]


def test_multi_source_isolates_failing_source():
    """One source raising (e.g. a real API failure) must not destroy
    results already gathered from the other, independent sources."""
    provider = MultiSourceUniverseProvider([_FailingProvider(), _ListProvider(["A", "B"])])
    assert provider.get_symbols() == ["A", "B"]


def test_multi_source_isolates_failing_source_regardless_of_position():
    provider = MultiSourceUniverseProvider([_ListProvider(["A", "B"]), _FailingProvider(), _ListProvider(["C"])])
    assert provider.get_symbols() == ["A", "B", "C"]


def test_multi_source_all_sources_failing_returns_empty():
    provider = MultiSourceUniverseProvider([_FailingProvider(), _FailingProvider()])
    assert provider.get_symbols() == []


def test_multi_source_with_no_providers_returns_empty():
    assert MultiSourceUniverseProvider([]).get_symbols() == []


# -- StaticUniverseProvider ---------------------------------------------------

def test_static_universe_provider_returns_fixed_list():
    provider = StaticUniverseProvider(["AAPL", "GME"])
    assert provider.get_symbols() == ["AAPL", "GME"]
