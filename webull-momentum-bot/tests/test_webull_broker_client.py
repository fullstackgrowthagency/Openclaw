"""
Hermetic tests for WebullBrokerClient's mapping/payload logic -- no real
network calls or credentials required. Fixtures mirror real response shapes
captured live against the Webull sandbox on 2026-08-08 (see
brokers/webull/client.py's module docstring for exactly what was verified
vs. best-effort/unverified).
"""
from datetime import datetime
from types import SimpleNamespace

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
    # "CORE" -- "ALL" was tried and confirmed live to be REJECTED by this
    # account/endpoint (417 OAUTH_OPENAPI_PARAM_ERR), contradicting Webull's
    # own public docs. See _order_payload's comment for the full history;
    # don't change this back to "ALL" without a live order proving it works.
    assert payload["support_trading_session"] == "CORE"


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


# -- get_positions -- one bad/unexpected row must never take down every
# other real position (a real production incident: see get_positions' and
# _position_from_dict's docstrings) --------------------------------------

def test_get_positions_maps_a_well_formed_row():
    client = _client()
    client.account_id = "test-account"

    class _FakeAccountV2:
        def get_account_position(self, account_id):
            return _FakeResponse([{"symbol": "AAPL", "quantity": "10", "cost_price": "150.5", "side": "BUY"}])

    class _FakeTradeClient:
        account_v2 = _FakeAccountV2()

    client._require_trade_client = lambda: _FakeTradeClient()
    positions = client.get_positions()

    assert len(positions) == 1
    assert positions[0].symbol == "AAPL"
    assert positions[0].quantity == 10.0
    assert positions[0].avg_entry_price == 150.5


def test_get_positions_skips_an_unparseable_row_but_keeps_the_rest():
    client = _client()
    client.account_id = "test-account"

    class _FakeAccountV2:
        def get_account_position(self, account_id):
            # First row has none of the recognized symbol keys (simulates a
            # field-name guess in _position_from_dict being wrong); second
            # row is well-formed. Both real positions -- the first must not
            # silently take the second down with it.
            return _FakeResponse([
                {"unexpected_symbol_field": "GME", "quantity": "5"},
                {"symbol": "AAPL", "quantity": "10", "cost_price": "150.5"},
            ])

    class _FakeTradeClient:
        account_v2 = _FakeAccountV2()

    client._require_trade_client = lambda: _FakeTradeClient()
    positions = client.get_positions()

    assert len(positions) == 1
    assert positions[0].symbol == "AAPL"


def test_get_positions_empty_list_returns_empty():
    client = _client()
    client.account_id = "test-account"

    class _FakeAccountV2:
        def get_account_position(self, account_id):
            return _FakeResponse([])

    class _FakeTradeClient:
        account_v2 = _FakeAccountV2()

    client._require_trade_client = lambda: _FakeTradeClient()
    assert client.get_positions() == []


# -- get_snapshots (batch equivalent of get_snapshot) -- see its docstring for
# why this exists: every get_snapshot-family call shares the same globally-
# paced rate limiter, so batching many symbols into one call turns an N-call
# floor into a ceil(N/100)-call one ------------------------------------------

def test_get_snapshots_empty_list_makes_no_calls():
    client = _client()
    calls = []

    class _FakeMarketData:
        def get_snapshot(self, symbols, category, **kwargs):
            calls.append(symbols)
            return _FakeResponse([])

    class _FakeDataClient:
        market_data = _FakeMarketData()

    client._require_data_client = lambda: _FakeDataClient()
    assert client.get_snapshots([]) == {}
    assert calls == []


def test_get_snapshots_maps_each_row_by_its_own_symbol():
    client = _client()

    class _FakeMarketData:
        def get_snapshot(self, symbols, category, **kwargs):
            rows = [dict(_REAL_SNAPSHOT_ROW, symbol=s, price=str(i + 1)) for i, s in enumerate(symbols)]
            return _FakeResponse(rows)

    class _FakeDataClient:
        market_data = _FakeMarketData()

    client._require_data_client = lambda: _FakeDataClient()
    results = client.get_snapshots(["AAPL", "GME"])

    assert set(results.keys()) == {"AAPL", "GME"}
    assert results["AAPL"].last_price == 1.0
    assert results["GME"].last_price == 2.0


def test_get_snapshots_omits_symbols_webull_did_not_return():
    client = _client()

    class _FakeMarketData:
        def get_snapshot(self, symbols, category, **kwargs):
            return _FakeResponse([dict(_REAL_SNAPSHOT_ROW, symbol="AAPL")])  # "GME" missing from the response

    class _FakeDataClient:
        market_data = _FakeMarketData()

    client._require_data_client = lambda: _FakeDataClient()
    results = client.get_snapshots(["AAPL", "GME"])

    assert set(results.keys()) == {"AAPL"}


def test_get_snapshots_chunks_requests_at_the_batch_size_cap():
    from webull_bot.brokers.webull.client import _SNAPSHOT_BATCH_SIZE

    client = _client()
    calls = []

    class _FakeMarketData:
        def get_snapshot(self, symbols, category, **kwargs):
            calls.append(list(symbols))
            return _FakeResponse([dict(_REAL_SNAPSHOT_ROW, symbol=s) for s in symbols])

    class _FakeDataClient:
        market_data = _FakeMarketData()

    client._require_data_client = lambda: _FakeDataClient()
    symbols = [f"SYM{i}" for i in range(_SNAPSHOT_BATCH_SIZE + 50)]
    results = client.get_snapshots(symbols)

    assert len(calls) == 2
    assert len(calls[0]) == _SNAPSHOT_BATCH_SIZE
    assert len(calls[1]) == 50
    assert len(results) == len(symbols)


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


def test_order_from_detail_parses_stop_price_when_present():
    client = _client()
    order = client._order_from_detail(
        {"symbol": "AAPL", "side": "SELL", "quantity": "10", "status": "SUBMITTED", "stop_price": "95.50"}
    )
    assert order.stop_price == 95.50


def test_order_from_detail_stop_price_defaults_to_none():
    client = _client()
    order = client._order_from_detail({"symbol": "AAPL", "side": "SELL", "quantity": "10", "status": "SUBMITTED"})
    assert order.stop_price is None


# -- OCO stop+target bracket (place_oco_bracket) -----------------------------

def _sandbox_client() -> WebullBrokerClient:
    from webull_bot.config import TradingMode

    client = _client()
    client.settings = SimpleNamespace(trading_mode=TradingMode.SANDBOX)
    client.account_id = "test-account"
    return client


def test_place_oco_bracket_sends_both_legs_as_one_combo():
    client = _sandbox_client()
    captured = {}

    class _FakeOrderV3:
        def place_order(self, account_id, order_dicts):
            captured["account_id"] = account_id
            captured["order_dicts"] = order_dicts
            return _FakeResponse([{"status": "accepted"}])

    class _FakeTradeClient:
        order_v3 = _FakeOrderV3()

    client._trade_client = _FakeTradeClient()

    stop_order = Order(symbol="AAPL", side=OrderSide.SELL, order_type=OrderType.STOP, quantity=2, stop_price=304.13)
    target_order = Order(symbol="AAPL", side=OrderSide.SELL, order_type=OrderType.LIMIT, quantity=2, limit_price=322.56)

    result_stop, result_target = client.place_oco_bracket(stop_order, target_order)

    assert captured["account_id"] == "test-account"
    stop_payload, target_payload = captured["order_dicts"]
    assert stop_payload["combo_type"] == "OCO"
    assert target_payload["combo_type"] == "OCO"
    # Both legs share exactly one combo id -- confirmed live this is what
    # ties them together as one OCO pair rather than two independent orders.
    assert stop_payload["client_combo_order_id"] == target_payload["client_combo_order_id"]
    assert stop_payload["order_type"] == "STOP_LOSS"
    assert stop_payload["stop_price"] == "304.13"
    assert target_payload["order_type"] == "LIMIT"
    assert target_payload["limit_price"] == "322.56"

    # Each leg gets its OWN client_order_id, distinct from the shared combo
    # id -- confirmed live that cancel_order needs a leg's own id, not the
    # combo-level id (see cancel_order's docstring/module docstring).
    assert stop_payload["client_order_id"] != target_payload["client_order_id"]
    assert result_stop.broker_order_id == stop_payload["client_order_id"]
    assert result_target.broker_order_id == target_payload["client_order_id"]
    assert result_stop.status == OrderStatus.SUBMITTED
    assert result_target.status == OrderStatus.SUBMITTED


def test_place_oco_bracket_refuses_when_live_trading_not_authorized():
    from webull_bot.config import TradingMode

    client = _client()
    client.settings = SimpleNamespace(
        trading_mode=TradingMode.LIVE, is_live_trading_authorized=lambda: False,
    )
    client.account_id = "test-account"
    stop_order = Order(symbol="AAPL", side=OrderSide.SELL, order_type=OrderType.STOP, quantity=1, stop_price=1.0)
    target_order = Order(symbol="AAPL", side=OrderSide.SELL, order_type=OrderType.LIMIT, quantity=1, limit_price=2.0)

    with pytest.raises(RuntimeError):
        client.place_oco_bracket(stop_order, target_order)
