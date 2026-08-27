"""
Hermetic tests for FMPFloatProvider -- no real network calls. The fixtures
below mirror the exact response shapes captured from FMP's live `stable`
API on 2026-08-08 (see fmp.py's module docstring).
"""
from dataclasses import dataclass

import pytest

from webull_bot.config import FMPCredentials
from webull_bot.data.float_providers.fmp import FMPFloatProvider


@dataclass
class _FakeResponse:
    _payload: object
    status_code: int = 200

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeHttp:
    """Routes by (URL path suffix, symbol query param), like the real FMP calls."""

    def __init__(self, responses: dict[str, object]):
        # responses keyed by path suffix ("shares-float", "profile"); each
        # value is either a payload used for every symbol, or a dict mapping
        # symbol -> payload for per-symbol control.
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, params=None, timeout=None):
        params = params or {}
        self.calls.append((url, params))
        symbol = params.get("symbol")
        for path_suffix, payload in self.responses.items():
            if not url.endswith(path_suffix):
                continue
            if isinstance(payload, dict) and symbol in payload:
                return _FakeResponse(payload[symbol])
            if isinstance(payload, dict) and "Error Message" in payload:
                return _FakeResponse(payload)
            if isinstance(payload, dict):
                return _FakeResponse([])
            return _FakeResponse(payload)
        return _FakeResponse([], status_code=404)


def _credentials() -> FMPCredentials:
    return FMPCredentials(api_key="test-key", base_url="https://financialmodelingprep.com/stable")


def test_get_float_data_maps_fields_correctly():
    fake_http = _FakeHttp(
        {
            "shares-float": [
                {
                    "symbol": "GME",
                    "date": "2026-08-07 22:33:49",
                    "freeFloat": 91.18524777116394,
                    "floatShares": 409140234,
                    "outstandingShares": 448691257,
                    "source": "https://www.sec.gov/example",
                }
            ],
            "profile": [{"symbol": "GME", "marketCap": 12_000_000_000}],
        }
    )
    provider = FMPFloatProvider(_credentials(), http_get=fake_http)

    data = provider.get_float_data("GME")

    assert data.symbol == "GME"
    assert data.free_float_shares == 409140234
    assert data.shares_outstanding == 448691257
    assert data.market_cap == 12_000_000_000
    assert round(data.float_percent, 4) == round(91.18524777116394 / 100.0, 4)
    assert data.effective_date is not None
    assert data.effective_date.year == 2026
    assert data.source == "fmp"


def test_get_float_data_raises_on_empty_response():
    fake_http = _FakeHttp({"shares-float": [], "profile": []})
    provider = FMPFloatProvider(_credentials(), http_get=fake_http)
    with pytest.raises(ValueError):
        provider.get_float_data("NOSYMBOL")


def test_get_float_data_raises_a_value_error_on_null_float_shares():
    # Real incident (2026-08-27): FMP can return a non-empty row for a
    # symbol with floatShares/outstandingShares present but null (small/
    # illiquid/newly-listed names) -- float(None) used to raise a raw
    # TypeError here instead of this method's documented ValueError "no
    # data" contract.
    fake_http = _FakeHttp(
        {"shares-float": [{"symbol": "CGCF", "floatShares": None, "outstandingShares": None}]},
    )
    provider = FMPFloatProvider(_credentials(), http_get=fake_http)

    with pytest.raises(ValueError, match="incomplete shares-float data"):
        provider.get_float_data("CGCF")


def test_get_float_data_survives_profile_failure():
    """Market cap is a nice-to-have; a broken /profile call must not block float data."""
    import requests

    def flaky_http(url, params=None, timeout=None):
        if url.endswith("profile"):
            raise requests.RequestException("simulated network failure")
        return _FakeResponse(
            [{"symbol": "ABCD", "date": "2026-01-01", "freeFloat": 50.0, "floatShares": 1_000_000, "outstandingShares": 2_000_000}]
        )

    provider = FMPFloatProvider(_credentials(), http_get=flaky_http)
    data = provider.get_float_data("ABCD")
    assert data.market_cap is None
    assert data.free_float_shares == 1_000_000


def test_get_float_data_bulk_skips_failures():
    fake_http = _FakeHttp(
        {
            "shares-float": {
                "OK": [
                    {"symbol": "OK", "date": "2026-01-01", "freeFloat": 20.0, "floatShares": 500_000, "outstandingShares": 2_500_000}
                ],
                "MISSING": [],
            },
            "profile": {"OK": [{"symbol": "OK", "marketCap": 1_000_000}], "MISSING": []},
        }
    )
    provider = FMPFloatProvider(_credentials(), http_get=fake_http)
    result = provider.get_float_data_bulk(["OK", "MISSING"])
    assert "OK" in result
    assert "MISSING" not in result


def test_raises_without_api_key():
    with pytest.raises(RuntimeError):
        FMPFloatProvider(FMPCredentials(api_key="", base_url="https://x"))


def test_error_message_payload_raises_runtime_error():
    fake_http = _FakeHttp({"shares-float": {"Error Message": "Legacy Endpoint"}})
    provider = FMPFloatProvider(_credentials(), http_get=fake_http)
    with pytest.raises(RuntimeError):
        provider.get_float_data("AAPL")
