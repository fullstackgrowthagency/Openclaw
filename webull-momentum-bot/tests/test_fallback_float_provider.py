"""
Hermetic tests for FallbackFloatProvider using tiny fake FloatDataProvider
stand-ins -- no real FMP/Yahoo network calls. See fallback.py's module
docstring for the primary-then-fallbacks-in-order contract this enforces.
"""
from datetime import datetime

import pytest

from webull_bot.data.float_providers.fallback import FallbackFloatProvider
from webull_bot.interfaces.float_provider import FloatDataProvider
from webull_bot.models import FloatData


def _float_data(symbol: str, source: str) -> FloatData:
    return FloatData(
        symbol=symbol, free_float_shares=1_000_000, shares_outstanding=2_000_000,
        market_cap=None, float_percent=0.5, effective_date=None, fetched_at=datetime.utcnow(), source=source,
    )


class _FakeProvider(FloatDataProvider):
    def __init__(self, name: str, data: dict[str, FloatData] | None = None, always_fails: bool = False):
        self.name = name
        self.data = data or {}
        self.always_fails = always_fails
        self.calls: list[str] = []

    def get_float_data(self, symbol: str) -> FloatData:
        self.calls.append(symbol)
        if self.always_fails or symbol not in self.data:
            raise RuntimeError(f"{self.name} has no data for {symbol}")
        return self.data[symbol]

    def get_float_data_bulk(self, symbols: list[str]) -> dict[str, FloatData]:
        self.calls.extend(symbols)
        return {s: self.data[s] for s in symbols if s in self.data and not self.always_fails}


def test_uses_primary_when_it_succeeds_without_touching_fallback():
    primary = _FakeProvider("primary", {"GME": _float_data("GME", "primary")})
    fallback = _FakeProvider("fallback")
    provider = FallbackFloatProvider(primary, [fallback])

    data = provider.get_float_data("GME")

    assert data.source == "primary"
    assert fallback.calls == []


def test_falls_back_when_primary_fails():
    primary = _FakeProvider("primary", always_fails=True)
    fallback = _FakeProvider("fallback", {"GME": _float_data("GME", "fallback")})
    provider = FallbackFloatProvider(primary, [fallback])

    data = provider.get_float_data("GME")

    assert data.source == "fallback"


def test_tries_fallbacks_in_order():
    primary = _FakeProvider("primary", always_fails=True)
    first_fallback = _FakeProvider("first", always_fails=True)
    second_fallback = _FakeProvider("second", {"GME": _float_data("GME", "second")})
    provider = FallbackFloatProvider(primary, [first_fallback, second_fallback])

    data = provider.get_float_data("GME")

    assert data.source == "second"
    assert first_fallback.calls == ["GME"]


def test_raises_when_all_providers_fail():
    primary = _FakeProvider("primary", always_fails=True)
    fallback = _FakeProvider("fallback", always_fails=True)
    provider = FallbackFloatProvider(primary, [fallback])

    with pytest.raises(RuntimeError):
        provider.get_float_data("GME")


def test_bulk_recovers_only_symbols_missing_from_primary():
    primary = _FakeProvider("primary", {"OK": _float_data("OK", "primary")})
    fallback = _FakeProvider("fallback", {"RECOVERED": _float_data("RECOVERED", "fallback")})
    provider = FallbackFloatProvider(primary, [fallback])

    result = provider.get_float_data_bulk(["OK", "RECOVERED", "NEVER"])

    assert result["OK"].source == "primary"
    assert result["RECOVERED"].source == "fallback"
    assert "NEVER" not in result
    # The fallback should only have been asked about the symbols primary missed.
    assert fallback.calls == ["RECOVERED", "NEVER"]


def test_bulk_with_no_fallbacks_returns_only_primary_results():
    primary = _FakeProvider("primary", {"OK": _float_data("OK", "primary")})
    provider = FallbackFloatProvider(primary, [])

    result = provider.get_float_data_bulk(["OK", "MISSING"])

    assert list(result.keys()) == ["OK"]
