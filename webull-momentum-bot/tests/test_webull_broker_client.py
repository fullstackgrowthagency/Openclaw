"""
Hermetic tests for WebullBrokerClient's mapping/payload logic -- no real
network calls or credentials required. Fixtures mirror real response shapes
captured live against the Webull sandbox on 2026-08-08 (see
brokers/webull/client.py's module docstring for exactly what was verified
vs. best-effort/unverified).
"""
from datetime import datetime

import pytest

from webull_bot.brokers.webull.client import WebullBrokerClient
from webull_bot.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from webull_bot.models import Order


def _client() -> WebullBrokerClient:
    # Bypasses __init__ (which requires configured Settings) since these
    # tests only exercise pure mapping/payload helpers.
    return WebullBrokerClient.__new__(WebullBrokerClient)


# -- order payload building (real request schema, verified live) -----------

def test_order_payload_maps_market_buy_correctly():
    client = _client()
    order = Order(
        symbol="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=10,
        time_in_force=TimeInForce.DAY,
        client_order_id="test-id-123",
    )
    payload = client._order_payload(order)
    assert payload["symbol"] == "AAPL"
    assert payload["instrument_type"] == "EQUITY"  # confirmed live; not the SDK's "STOCK" enum name
    assert payload["market"] == "US"
    assert payload["order_type"] == "MARKET"
    assert payload["side"] == "BUY"
    assert payload["time_in_force"] == "DAY"
    assert payload["quantity"] == "10"
    assert payload["client_order_id"] == "test-id-123"
    assert "limit_price" not in payload


def test_order_payload_includes_limit_price_when_set():
    client = _client()
    order = Order(
        symbol="GME", side=OrderSide.SELL, order_type=OrderType.LIMIT,
        quantity=5, limit_price=25.50, client_order_id="x",
    )
    payload = client._order_payload(order)
    assert payload["order_type"] == "LIMIT"
    assert payload["limit_price"] == "25.5"


def test_order_payload_maps_sell_short_to_short_side():
    client = _client()
    order = Order(
        symbol="GME", side=OrderSide.SELL_SHORT, order_type=OrderType.MARKET,
        quantity=1, client_order_id="x",
    )
    payload = client._order_payload(order)
    assert payload["side"] == "SHORT"


def test_order_payload_generates_client_order_id_when_missing():
    client = _client()
    order = Order(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=1)
    payload = client._order_payload(order)
    assert payload["client_order_id"]


def test_order_payload_rejects_fok_time_in_force():
    """Webull's SDK has no FOK member in order_tif.py -- confirmed, not an oversight."""
    client = _client()
    order = Order(
        symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.MARKET,
        quantity=1, time_in_force=TimeInForce.FOK,
    )
    with pytest.raises(ValueError):
        client._order_payload(order)


# -- snapshot mapping (real field names, verified live against AAPL) -------

_REAL_SNAPSHOT_ROW = {
    "symbol": "AAPL", "price": "313.33", "open": "311.450000", "high": "314.810000",
    "low": "310.740000", "volume": "34437191", "close": "313.33", "pre_close": "312.410000",
    "ask": "313.50", "ask_size": "200", "bid": "311.19", "bid_size": "9",
    "quote_time": 1786147200276,
}


def test_snapshot_from_dict_maps_real_fields():
    client = _client()
    snapshot = client._snapshot_from_dict(_REAL_SNAPSHOT_ROW)
    assert snapshot.symbol == "AAPL"
    assert snapshot.last_price == 313.33
    assert snapshot.bid == 311.19
    assert snapshot.ask == 313.50
    assert snapshot.high_of_day == 314.81
    assert snapshot.low_of_day == 310.74
    assert snapshot.prev_close == 312.41
    # VWAP is not returned by this endpoint (confirmed live) -- falls back to last_price.
    assert snapshot.vwap == snapshot.last_price
    assert snapshot.premarket_high is None


# -- bar mapping (real field names, verified live; most-recent-first input) -

_REAL_BARS_MOST_RECENT_FIRST = [
    {"symbol": "AAPL", "time": "2026-08-07T19:59:00.000+0000", "open": "313.65",
     "close": "313.33", "high": "313.75", "low": "313.22", "volume": "7748730"},
    {"symbol": "AAPL", "time": "2026-08-07T19:58:00.000+0000", "open": "313.86",
     "close": "313.655", "high": "313.8742", "low": "313.655", "volume": "370835"},
    {"symbol": "AAPL", "time": "2026-08-07T19:57:00.000+0000", "open": "313.875",
     "close": "313.86", "high": "313.98", "low": "313.79", "volume": "225405"},
]


def test_snapshots_from_bars_reorders_to_chronological():
    client = _client()
    snapshots = client._snapshots_from_bars("AAPL", _REAL_BARS_MOST_RECENT_FIRST)
    timestamps = [s.timestamp for s in snapshots]
    assert timestamps == sorted(timestamps)
    assert snapshots[0].timestamp < snapshots[-1].timestamp


def test_snapshots_from_bars_computes_running_cumulative_volume():
    client = _client()
    snapshots = client._snapshots_from_bars("AAPL", _REAL_BARS_MOST_RECENT_FIRST)
    # Chronological order: 19:57 (225405) -> 19:58 (370835) -> 19:59 (7748730)
    assert snapshots[0].cumulative_volume == 225405.0
    assert snapshots[1].cumulative_volume == 225405.0 + 370835.0
    assert snapshots[2].cumulative_volume == 225405.0 + 370835.0 + 7748730.0


def test_snapshots_from_bars_running_high_low_are_monotonic():
    client = _client()
    snapshots = client._snapshots_from_bars("AAPL", _REAL_BARS_MOST_RECENT_FIRST)
    highs = [s.high_of_day for s in snapshots]
    lows = [s.low_of_day for s in snapshots]
    assert highs == sorted(highs)  # running max never decreases
    assert lows == sorted(lows, reverse=True)  # running min never increases
    assert snapshots[-1].high_of_day == max(float(b["high"]) for b in _REAL_BARS_MOST_RECENT_FIRST)


def test_get_bars_rejects_unsupported_interval():
    client = _client()
    with pytest.raises(ValueError):
        client.get_bars("AAPL", "3m", 10)


# -- order status mapping ---------------------------------------------------

def test_order_from_detail_maps_status():
    client = _client()
    order = client._order_from_detail(
        {"symbol": "AAPL", "side": "BUY", "quantity": "10", "status": "FILLED", "client_order_id": "abc"}
    )
    assert order.status == OrderStatus.FILLED
    assert order.broker_order_id == "abc"


def test_order_from_detail_unknown_status_falls_back_to_pending():
    client = _client()
    order = client._order_from_detail({"symbol": "AAPL", "side": "BUY", "quantity": "1", "status": "SOMETHING_NEW"})
    assert order.status == OrderStatus.PENDING
