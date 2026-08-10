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


# -- extended-hours price/volume preference (ext_price/ext_volume), gated to
# outside the regular 9:30am-4:00pm ET session -- see get_snapshot's
# extend_hour_required=True and _snapshot_from_dict's docstring for why the
# exact REST field names here are inferred, not confirmed live, and why the
# time gate exists (protects regular-session data from a stale pre-market
# value in case Webull doesn't zero these fields out at the open) ----------

# _REAL_SNAPSHOT_ROW's quote_time (1786147200276) is 2026-08-07 20:00 ET --
# after-hours -- which is why the tests below that rely on the ext_* fields
# actually taking effect use it as-is, with no override needed.

def test_snapshot_from_dict_falls_back_to_price_without_ext_price():
    # _REAL_SNAPSHOT_ROW has no ext_price key at all (captured before
    # extend_hour_required=True was added) -- must not raise or misbehave.
    client = _client()
    snapshot = client._snapshot_from_dict(_REAL_SNAPSHOT_ROW)
    assert snapshot.last_price == 313.33


def test_snapshot_from_dict_prefers_ext_price_when_present():
    client = _client()
    row = dict(_REAL_SNAPSHOT_ROW, ext_price="320.00")
    snapshot = client._snapshot_from_dict(row)
    assert snapshot.last_price == 320.00
    # VWAP's fallback-to-last_price should follow the extended-hours price too.
    assert snapshot.vwap == 320.00


def test_snapshot_from_dict_ignores_zero_or_empty_ext_price():
    client = _client()
    for zero_ish in ("0", 0, 0.0, ""):
        row = dict(_REAL_SNAPSHOT_ROW, ext_price=zero_ish)
        snapshot = client._snapshot_from_dict(row)
        assert snapshot.last_price == 313.33


def test_snapshot_from_dict_ignores_unparseable_ext_price():
    client = _client()
    row = dict(_REAL_SNAPSHOT_ROW, ext_price="not-a-number")
    snapshot = client._snapshot_from_dict(row)
    assert snapshot.last_price == 313.33


def test_snapshot_from_dict_prefers_ext_volume_when_present():
    client = _client()
    row = dict(_REAL_SNAPSHOT_ROW, ext_volume="999")
    snapshot = client._snapshot_from_dict(row)
    assert snapshot.cumulative_volume == 999.0


def test_snapshot_from_dict_falls_back_to_volume_without_ext_volume():
    client = _client()
    snapshot = client._snapshot_from_dict(_REAL_SNAPSHOT_ROW)
    assert snapshot.cumulative_volume == 34437191.0


def test_snapshot_from_dict_ignores_zero_or_empty_ext_volume():
    client = _client()
    for zero_ish in ("0", 0, 0.0, ""):
        row = dict(_REAL_SNAPSHOT_ROW, ext_volume=zero_ish)
        snapshot = client._snapshot_from_dict(row)
        assert snapshot.cumulative_volume == 34437191.0


# 2026-08-07 12:00 ET -- squarely inside the regular session.
_REGULAR_HOURS_QUOTE_TIME_MS = 1786118400000
# 2026-08-07 08:00 ET -- pre-market.
_PREMARKET_QUOTE_TIME_MS = 1786104000000


def test_snapshot_from_dict_ignores_ext_price_during_regular_hours():
    # Even a present, non-zero, parseable ext_price must NOT override price
    # if the quote itself is timestamped during the regular session -- this
    # is the gate that protects against Webull possibly not zeroing ext_price
    # out once the regular session opens (unconfirmed either way).
    client = _client()
    row = dict(_REAL_SNAPSHOT_ROW, ext_price="320.00", quote_time=_REGULAR_HOURS_QUOTE_TIME_MS)
    snapshot = client._snapshot_from_dict(row)
    assert snapshot.last_price == 313.33


def test_snapshot_from_dict_ignores_ext_volume_during_regular_hours():
    client = _client()
    row = dict(_REAL_SNAPSHOT_ROW, ext_volume="999", quote_time=_REGULAR_HOURS_QUOTE_TIME_MS)
    snapshot = client._snapshot_from_dict(row)
    assert snapshot.cumulative_volume == 34437191.0


def test_snapshot_from_dict_honors_ext_fields_during_premarket():
    client = _client()
    row = dict(
        _REAL_SNAPSHOT_ROW, ext_price="320.00", ext_volume="999", quote_time=_PREMARKET_QUOTE_TIME_MS,
    )
    snapshot = client._snapshot_from_dict(row)
    assert snapshot.last_price == 320.00
    assert snapshot.cumulative_volume == 999.0


def test_is_outside_regular_session_boundaries():
    from webull_bot.brokers.webull.client import _is_outside_regular_session

    # 9:29:59am ET -- one second before the open.
    assert _is_outside_regular_session(datetime(2026, 8, 7, 13, 29, 59)) is True
    # 9:30:00am ET -- the open itself, regular session.
    assert _is_outside_regular_session(datetime(2026, 8, 7, 13, 30, 0)) is False
    # 3:59:59pm ET -- still regular session.
    assert _is_outside_regular_session(datetime(2026, 8, 7, 19, 59, 59)) is False
    # 4:00:00pm ET -- the close itself, after-hours begins.
    assert _is_outside_regular_session(datetime(2026, 8, 7, 20, 0, 0)) is True


class _FakeResponse:
    def __init__(self, rows):
        self._rows = rows

    def raise_for_status(self):
        pass

    def json(self):
        return self._rows


def test_get_snapshot_requests_extended_hours_data():
    client = _client()
    calls = []

    class _FakeMarketData:
        def get_snapshot(self, symbols, category, **kwargs):
            calls.append((symbols, category, kwargs))
            return _FakeResponse([dict(_REAL_SNAPSHOT_ROW)])

    class _FakeDataClient:
        market_data = _FakeMarketData()

    client._require_data_client = lambda: _FakeDataClient()
    client.get_snapshot("AAPL")

    assert len(calls) == 1
    _, _, kwargs = calls[0]
    assert kwargs.get("extend_hour_required") is True


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


def test_get_raw_bars_requests_all_trading_sessions():
    # See get_raw_bars' docstring for why "PRE"/"RTH"/"ATH" is inferred by
    # analogy from a sibling SDK endpoint (get_footprint), not confirmed
    # against a real get_history_bar response -- this locks in the request
    # shape so a regression is caught even before that live re-verification.
    client = _client()
    calls = []

    class _FakeMarketData:
        def get_history_bar(self, symbol, category, timespan, **kwargs):
            calls.append((symbol, category, timespan, kwargs))

            class _Resp:
                def raise_for_status(self):
                    pass

                def json(self):
                    return list(_REAL_BARS_MOST_RECENT_FIRST)
            return _Resp()

    class _FakeDataClient:
        market_data = _FakeMarketData()

    client._require_data_client = lambda: _FakeDataClient()
    client.get_raw_bars("AAPL", "5m", 780)

    assert len(calls) == 1
    _, _, _, kwargs = calls[0]
    assert kwargs.get("trading_sessions") == ["PRE", "RTH", "ATH"]


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
