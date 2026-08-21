"""
Hermetic tests for WebullBrokerClient's mapping/payload logic -- no real
network calls or credentials required. Fixtures mirror real response shapes
captured live against the Webull sandbox on 2026-08-08 (see
brokers/webull/client.py's module docstring for exactly what was verified
vs. best-effort/unverified).
"""
from collections import deque
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Optional

import pytest

from webull_bot.brokers.webull import retry as retry_module
from webull_bot.brokers.webull.client import WebullBrokerClient
from webull_bot.brokers.webull.retry import RateLimiter
from webull_bot.enums import OrderSide, OrderStatus, OrderType, TimeInForce, TradeSide
from webull_bot.models import MarketSnapshot, Order, TickRecord


@pytest.fixture(autouse=True)
def _no_rate_limit_delay():
    """Every WebullBrokerClient method that talks to the SDK goes through
    a real RateLimiter (client.py's own self._limiter, one per instance
    since the 2026-08-15 multi-tenant conversion -- see __init__) -- fine
    and correct for production, but these tests fake the SDK client
    entirely, so waiting out real pacing here would only slow the suite
    down for no reason. _client() below constructs that per-instance
    limiter with min_interval_seconds=0.0 for exactly this reason; this
    fixture is now a no-op placeholder kept so `autouse` call sites/history
    don't need touching, but real zero-delay setup lives in _client()."""
    yield


def _client() -> WebullBrokerClient:
    # Bypasses __init__ (which requires configured Settings) since these
    # tests only exercise pure mapping/payload helpers. `settings` is set to
    # a minimal stub (not the real dataclass) since _order_payload reads
    # `self.settings.webull_support_trading_session` (2026-08-12) -- tests
    # further down the file that need other Settings fields already
    # reassign client.settings themselves, which simply overwrites this.
    client = WebullBrokerClient.__new__(WebullBrokerClient)
    client.settings = SimpleNamespace(webull_support_trading_session="CORE")
    # Zero-interval instance limiter (2026-08-15) -- __init__ is bypassed
    # above, so this must be set explicitly for every place_order/
    # place_oco_bracket/place_bracket_entry .exclusive() call and every
    # call_with_retry(..., limiter=self._limiter,...) call site to work at
    # all, and 0.0 (not the real 1.0s) so these hermetic tests don't pay
    # real pacing delay against a limiter nothing else shares.
    client._limiter = RateLimiter(min_interval_seconds=0.0)
    return client


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


def test_order_payload_reads_support_trading_session_from_settings():
    # 2026-08-12: no longer hardcoded -- reads Settings.
    # webull_support_trading_session (env var
    # WEBULL_SUPPORT_TRADING_SESSION), so a candidate value can be tested
    # live without a code change. Default is still "CORE" (see the test
    # above); this confirms the plumbing itself, independent of whichever
    # value ends up being the default. created_at is pinned outside core
    # hours (7am ET) since the configured value is now only honored there
    # -- see the two tests below for the core-hours override itself.
    client = _client()
    client.settings = SimpleNamespace(webull_support_trading_session="ALL")
    order = Order(
        symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=10,
        created_at=datetime(2026, 1, 14, 12, 0, 0),  # 7:00am ET Wednesday -- pre-market
    )
    payload = client._order_payload(order)
    assert payload["support_trading_session"] == "ALL"


def test_order_payload_forces_core_session_during_core_hours_regardless_of_setting():
    # Confirmed live 2026-08-12 (post sandbox-reset, ~9:49am ET, genuinely
    # inside core hours): "ALL" is REJECTED during core hours specifically
    # (417 OAUTH_OPENAPI_PARAM_ERR), even though the same value is accepted
    # outside core hours (see the pre-market confirmation this same day,
    # documented in _order_payload's comment). A
    # WEBULL_SUPPORT_TRADING_SESSION=ALL deployment must still submit
    # "CORE" for any order placed during core hours or every core-hours
    # entry/exit fails with this param error.
    client = _client()
    client.settings = SimpleNamespace(webull_support_trading_session="ALL")
    order = Order(
        symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=10,
        created_at=datetime(2026, 1, 14, 15, 0, 0),  # 10:00am ET Wednesday -- core hours
    )
    payload = client._order_payload(order)
    assert payload["support_trading_session"] == "CORE"


def test_order_payload_uses_configured_session_outside_core_hours():
    client = _client()
    client.settings = SimpleNamespace(webull_support_trading_session="ALL")
    order = Order(
        symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=10,
        created_at=datetime(2026, 1, 14, 21, 0, 0),  # 4:00pm ET Wednesday -- just after close
    )
    payload = client._order_payload(order)
    assert payload["support_trading_session"] == "ALL"


def test_order_payload_includes_limit_price_when_set():
    client = _client()
    order = Order(
        symbol="GME", side=OrderSide.SELL, order_type=OrderType.LIMIT,
        quantity=5, limit_price=25.50, client_order_id="x",
    )
    payload = client._order_payload(order)
    assert payload["order_type"] == "LIMIT"
    assert payload["limit_price"] == "25.5"


def test_order_payload_rounds_limit_price_to_cents_at_or_above_a_dollar():
    # Confirmed live 2026-08-12: a resting OCO bracket's target/LIMIT leg
    # for BIVI was rejected (OAUTH_OPENAPI_STOCK_ORDER_PRICE_PRECISION_EXCEED,
    # HTTP 417, "Price increment should be 0.01 when price is equal to or
    # greater than 0.9999") because target_price -- computed as
    # entry_price + risk_per_share * reward_risk_ratio by every strategy,
    # none of which round -- came out as 3.4667600000000003, an unrounded
    # float with far more than 2 decimal digits.
    client = _client()
    order = Order(
        symbol="BIVI", side=OrderSide.SELL, order_type=OrderType.LIMIT,
        quantity=100, limit_price=3.4667600000000003, client_order_id="x",
    )
    payload = client._order_payload(order)
    assert payload["limit_price"] == "3.47"


def test_order_payload_rounds_stop_price_to_cents_at_or_above_a_dollar():
    client = _client()
    order = Order(
        symbol="BIVI", side=OrderSide.SELL, order_type=OrderType.STOP,
        quantity=100, stop_price=3.171999999999999, client_order_id="x",
    )
    payload = client._order_payload(order)
    assert payload["stop_price"] == "3.17"


def test_order_payload_rounds_sub_dollar_prices_to_four_decimals():
    # Sub-penny quoting is allowed below $1 -- this shouldn't be rounded
    # down to the nearest cent the way an at-or-above-$1 price is.
    client = _client()
    order = Order(
        symbol="DOGZ", side=OrderSide.SELL, order_type=OrderType.LIMIT,
        quantity=100, limit_price=0.05123456, client_order_id="x",
    )
    payload = client._order_payload(order)
    assert payload["limit_price"] == "0.0512"


def test_order_payload_includes_trailing_fields_when_set():
    # NOT YET independently confirmed live for a US-market equity on this
    # account -- see _ORDER_TYPE_TO_WEBULL's docstring and
    # scripts/verify_trailing_stop.py. This only tests that the payload
    # this codebase WOULD send matches the SDK's documented field names
    # (webull.trade.request.place_order_request.py's set_trailing_type/
    # set_trailing_stop_step), not that Webull accepts it.
    client = _client()
    order = Order(
        symbol="GME", side=OrderSide.SELL, order_type=OrderType.TRAILING_STOP,
        quantity=100, trailing_pct=3.0, client_order_id="x",
    )
    payload = client._order_payload(order)
    assert payload["order_type"] == "TRAILING_STOP_LOSS"
    assert payload["trailing_type"] == "PERCENTAGE"
    assert payload["trailing_stop_step"] == "3.0"
    assert "stop_price" not in payload


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


# -- _snapshot_from_dict tolerant price fallback (2026-08-21, real
# incident: a live position, PMI, never got its take-profit exit checked
# because Webull returned a snapshot row missing "price", which used to
# raise KeyError on every single call for that symbol, forever -- and
# because get_snapshots (below) had no per-row isolation, this could
# silently freeze the whole batch tick, not just that one symbol) --------

def test_snapshot_from_dict_falls_back_to_bid_ask_midpoint_without_price():
    client = _client()
    row = dict(_REAL_SNAPSHOT_ROW)
    del row["price"]
    snapshot = client._snapshot_from_dict(row)
    assert snapshot.last_price == pytest.approx((311.19 + 313.50) / 2.0)


def test_snapshot_from_dict_falls_back_to_pre_close_without_price_bid_or_ask():
    client = _client()
    row = dict(_REAL_SNAPSHOT_ROW)
    for key in ("price", "bid", "ask"):
        del row[key]
    snapshot = client._snapshot_from_dict(row)
    assert snapshot.last_price == 312.41


def test_snapshot_from_dict_raises_when_nothing_usable_is_present():
    client = _client()
    row = dict(_REAL_SNAPSHOT_ROW)
    for key in ("price", "bid", "ask", "pre_close"):
        del row[key]
    with pytest.raises(ValueError, match="no usable price"):
        client._snapshot_from_dict(row)


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


def test_get_snapshots_skips_a_malformed_row_without_losing_the_rest_of_the_batch():
    # Real incident (2026-08-21, PMI): before this fix, one row that failed
    # to parse (e.g. missing price/bid/ask/pre_close) raised out of the
    # whole get_snapshots call, discarding every other symbol's snapshot in
    # the same chunk too -- not just the bad symbol's.
    client = _client()

    class _FakeMarketData:
        def get_snapshot(self, symbols, category, **kwargs):
            good = dict(_REAL_SNAPSHOT_ROW, symbol="AAPL")
            bad = {"symbol": "PMI"}  # no price, bid, ask, or pre_close
            return _FakeResponse([good, bad])

    class _FakeDataClient:
        market_data = _FakeMarketData()

    client._require_data_client = lambda: _FakeDataClient()
    results = client.get_snapshots(["AAPL", "PMI"])

    assert set(results.keys()) == {"AAPL"}
    assert results["AAPL"].last_price == 313.33


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
    client.settings = SimpleNamespace(trading_mode=TradingMode.SANDBOX, webull_support_trading_session="CORE")
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


# -- atomic bracket entry (place_bracket_entry, 2026-08-13) ------------------

def test_place_bracket_entry_sends_all_three_legs_as_one_combo():
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

    entry_order = Order(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=10)
    stop_order = Order(symbol="AAPL", side=OrderSide.SELL, order_type=OrderType.STOP, quantity=10, stop_price=304.13)
    target_order = Order(symbol="AAPL", side=OrderSide.SELL, order_type=OrderType.LIMIT, quantity=5, limit_price=322.56)

    result_entry, result_stop, result_target = client.place_bracket_entry(entry_order, stop_order, target_order)

    assert captured["account_id"] == "test-account"
    entry_payload, stop_payload, target_payload = captured["order_dicts"]
    assert entry_payload["combo_type"] == "MASTER"
    assert stop_payload["combo_type"] == "STOP_LOSS"
    assert target_payload["combo_type"] == "STOP_PROFIT"
    # All three legs share exactly one combo id -- ties them together as
    # one atomic bracket entry rather than three independent orders.
    assert entry_payload["client_combo_order_id"] == stop_payload["client_combo_order_id"]
    assert entry_payload["client_combo_order_id"] == target_payload["client_combo_order_id"]
    assert entry_payload["order_type"] == "MARKET"
    assert entry_payload["side"] == "BUY"
    assert stop_payload["order_type"] == "STOP_LOSS"
    assert stop_payload["stop_price"] == "304.13"
    assert target_payload["order_type"] == "LIMIT"
    assert target_payload["limit_price"] == "322.56"

    # Each leg gets its own client_order_id, distinct from the shared combo id.
    assert len({entry_payload["client_order_id"], stop_payload["client_order_id"], target_payload["client_order_id"]}) == 3
    assert result_entry.broker_order_id == entry_payload["client_order_id"]
    assert result_stop.broker_order_id == stop_payload["client_order_id"]
    assert result_target.broker_order_id == target_payload["client_order_id"]
    assert result_entry.status == OrderStatus.SUBMITTED
    assert result_stop.status == OrderStatus.SUBMITTED
    assert result_target.status == OrderStatus.SUBMITTED


def test_place_bracket_entry_omits_target_leg_when_none():
    client = _sandbox_client()
    captured = {}

    class _FakeOrderV3:
        def place_order(self, account_id, order_dicts):
            captured["order_dicts"] = order_dicts
            return _FakeResponse([{"status": "accepted"}])

    class _FakeTradeClient:
        order_v3 = _FakeOrderV3()

    client._trade_client = _FakeTradeClient()

    entry_order = Order(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=1)
    stop_order = Order(symbol="AAPL", side=OrderSide.SELL, order_type=OrderType.STOP, quantity=1, stop_price=304.13)

    result_entry, result_stop, result_target = client.place_bracket_entry(entry_order, stop_order, None)

    # Only 2 legs sent -- no STOP_PROFIT leg at all, not a zero-share one.
    assert len(captured["order_dicts"]) == 2
    assert result_target is None


def test_place_bracket_entry_propagates_rejection_without_swallowing():
    client = _sandbox_client()

    class _FakeOrderV3:
        def place_order(self, account_id, order_dicts):
            raise RuntimeError("OAUTH_OPENAPI_PARAM_ERR: combo not supported for this instrument")

    class _FakeTradeClient:
        order_v3 = _FakeOrderV3()

    client._trade_client = _FakeTradeClient()

    entry_order = Order(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=1)
    stop_order = Order(symbol="AAPL", side=OrderSide.SELL, order_type=OrderType.STOP, quantity=1, stop_price=1.0)
    target_order = Order(symbol="AAPL", side=OrderSide.SELL, order_type=OrderType.LIMIT, quantity=1, limit_price=2.0)

    # No try/except inside place_bracket_entry -- a rejection must reach the
    # caller as a real exception, never silently return something usable.
    with pytest.raises(RuntimeError, match="combo not supported"):
        client.place_bracket_entry(entry_order, stop_order, target_order)


def test_place_bracket_entry_refuses_when_live_trading_not_authorized():
    from webull_bot.config import TradingMode

    client = _client()
    client.settings = SimpleNamespace(
        trading_mode=TradingMode.LIVE, is_live_trading_authorized=lambda: False,
    )
    client.account_id = "test-account"
    entry_order = Order(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=1)
    stop_order = Order(symbol="AAPL", side=OrderSide.SELL, order_type=OrderType.STOP, quantity=1, stop_price=1.0)

    with pytest.raises(RuntimeError):
        client.place_bracket_entry(entry_order, stop_order, None)


# -- priority tiers actually reach the shared limiter (2026-08-11) ----------
# Every Webull call this client makes -- not just market_data.* -- now goes
# through retry.call_with_retry with an explicit priority (see client.py's
# "Priority-tiered rate limiting" docstring note and retry.py's
# CallPriority). These tests spy on client.py's own `call_with_retry` name
# (not the SDK calls themselves, already covered above) to confirm each
# method requests the tier its docstring/comment claims, rather than
# silently falling back to call_with_retry's own NORMAL default.

def _spy_call_with_retry(monkeypatch):
    from webull_bot.brokers.webull import client as client_module

    calls = []
    real = client_module.call_with_retry

    def _spy(fn, **kwargs):
        calls.append(kwargs.get("priority"))
        return real(fn, **kwargs)

    monkeypatch.setattr(client_module, "call_with_retry", _spy)
    return calls


def test_get_positions_uses_critical_priority(monkeypatch):
    calls = _spy_call_with_retry(monkeypatch)
    client = _client()
    client.account_id = "test-account"

    class _FakeAccountV2:
        def get_account_position(self, account_id):
            return _FakeResponse([])

    class _FakeTradeClient:
        account_v2 = _FakeAccountV2()

    client._require_trade_client = lambda: _FakeTradeClient()
    client.get_positions()

    assert calls == [retry_module.CallPriority.CRITICAL]


def test_account_balance_uses_normal_priority(monkeypatch):
    calls = _spy_call_with_retry(monkeypatch)
    client = _client()
    client.account_id = "test-account"

    class _FakeAccountV2:
        def get_account_balance(self, account_id):
            return _FakeResponse({"account_currency_assets": [{"net_liquidation_value": "1000", "buying_power": "500"}]})

    class _FakeTradeClient:
        account_v2 = _FakeAccountV2()

    client._require_trade_client = lambda: _FakeTradeClient()
    client.get_account_equity()

    assert calls == [retry_module.CallPriority.NORMAL]


def test_place_order_uses_critical_priority(monkeypatch):
    calls = _spy_call_with_retry(monkeypatch)
    client = _client()
    client.settings = SimpleNamespace(
        trading_mode=None, is_live_trading_authorized=lambda: True, webull_support_trading_session="CORE",
    )
    client.account_id = "test-account"

    class _FakeOrderV3:
        def place_order(self, account_id, order_dicts):
            return _FakeResponse({"status": "accepted"})

    class _FakeTradeClient:
        order_v3 = _FakeOrderV3()

    client._require_trade_client = lambda: _FakeTradeClient()
    order = Order(symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=1)
    client.place_order(order)

    assert calls == [retry_module.CallPriority.CRITICAL]


def test_place_order_never_overlaps_across_different_symbols():
    # Real request (2026-08-13): orders for two or more different symbols
    # on the SAME account must never be placed at the broker at the same
    # time. Guaranteed by self._limiter.exclusive() -- not scoped per
    # symbol or per thread, so any two place_order calls through the SAME
    # WebullBrokerClient instance serialize regardless of which symbol
    # they're for. Proven here directly at the WebullBrokerClient level
    # (not just the abstract RateLimiter level -- see test_retry.py's
    # test_exclusive_serializes_concurrent_holders).
    #
    # 2026-08-15 UPDATE (multi-tenant conversion): the lock moved from a
    # single process-wide `webull_limiter` singleton to one instance PER
    # WebullBrokerClient (see client.py's __init__) -- correct, since each
    # user's own client now paces against their own account's rate limit,
    # not a shared one. This test uses ONE client instance across all
    # three threads (matching real production: TradingLoop builds exactly
    # one WebullBrokerClient per account and every symbol for that account
    # goes through it), which is exactly the scope self._limiter needs to
    # cover -- unlike the old singleton, it's no longer expected (or
    # correct) for two DIFFERENT client instances/accounts to serialize
    # against each other at all.
    import threading
    import time

    active: list[str] = []
    max_concurrent = 0
    lock = threading.Lock()

    class _SlowOrderV3:
        def place_order(self, account_id, order_dicts):
            nonlocal max_concurrent
            symbol = order_dicts[0]["symbol"]
            with lock:
                active.append(symbol)
                max_concurrent = max(max_concurrent, len(active))
            time.sleep(0.05)
            with lock:
                active.remove(symbol)
            return _FakeResponse({"status": "accepted"})

    class _FakeTradeClient:
        order_v3 = _SlowOrderV3()

    client = _client()
    client.settings = SimpleNamespace(
        trading_mode=None, is_live_trading_authorized=lambda: True, webull_support_trading_session="CORE",
    )
    client.account_id = "test-account"
    client._require_trade_client = lambda: _FakeTradeClient()

    def _place(symbol: str) -> None:
        order = Order(symbol=symbol, side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=1)
        client.place_order(order)

    threads = [threading.Thread(target=_place, args=(symbol,)) for symbol in ("AAPL", "TSLA", "GME")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=2.0)

    assert max_concurrent == 1


def test_cancel_order_uses_critical_priority(monkeypatch):
    calls = _spy_call_with_retry(monkeypatch)
    client = _client()
    client.account_id = "test-account"

    class _FakeOrderV3:
        def cancel_order(self, account_id, broker_order_id):
            return _FakeResponse({"status": "cancelled"})

    class _FakeTradeClient:
        order_v3 = _FakeOrderV3()

    client._require_trade_client = lambda: _FakeTradeClient()
    client.cancel_order("some-order-id")

    assert calls == [retry_module.CallPriority.CRITICAL]


def test_get_order_status_uses_critical_priority(monkeypatch):
    calls = _spy_call_with_retry(monkeypatch)
    client = _client()
    client.account_id = "test-account"

    class _FakeOrderV3:
        def get_order_detail(self, account_id, broker_order_id):
            return _FakeResponse({"symbol": "AAPL", "side": "BUY", "quantity": "1", "status": "FILLED"})

    class _FakeTradeClient:
        order_v3 = _FakeOrderV3()

    client._require_trade_client = lambda: _FakeTradeClient()
    client.get_order_status("some-order-id")

    assert calls == [retry_module.CallPriority.CRITICAL]


def test_modify_order_uses_normal_priority(monkeypatch):
    calls = _spy_call_with_retry(monkeypatch)
    client = _client()
    client.account_id = "test-account"

    class _FakeOrderV3:
        def replace_order(self, account_id, modify_dicts):
            return _FakeResponse({"status": "accepted"})

        def get_order_detail(self, account_id, broker_order_id):
            return _FakeResponse({"symbol": "AAPL", "side": "BUY", "quantity": "1", "status": "SUBMITTED"})

    class _FakeTradeClient:
        order_v3 = _FakeOrderV3()

    client._require_trade_client = lambda: _FakeTradeClient()
    client.modify_order("some-order-id", stop_price="10.00")

    # One NORMAL for replace_order, then one CRITICAL for the trailing
    # get_order_status readback (modify_order calls it directly) -- both
    # priorities are exercised here, not just the first call.
    assert calls == [retry_module.CallPriority.NORMAL, retry_module.CallPriority.CRITICAL]


def test_poll_fills_uses_normal_priority(monkeypatch):
    calls = _spy_call_with_retry(monkeypatch)
    client = _client()
    client.account_id = "test-account"

    class _FakeOrderV3:
        def get_order_executions(self, account_id, start_date=None):
            return _FakeResponse([])

    class _FakeTradeClient:
        order_v3 = _FakeOrderV3()

    client._require_trade_client = lambda: _FakeTradeClient()
    client.poll_fills()

    assert calls == [retry_module.CallPriority.NORMAL]


def test_get_snapshot_uses_normal_priority(monkeypatch):
    calls = _spy_call_with_retry(monkeypatch)
    client = _client()

    class _FakeMarketData:
        def get_snapshot(self, symbols, category, extend_hour_required=False):
            return _FakeResponse([_REAL_SNAPSHOT_ROW])

    class _FakeDataClient:
        market_data = _FakeMarketData()

    client._require_data_client = lambda: _FakeDataClient()
    client.get_snapshot("AAPL")

    assert calls == [retry_module.CallPriority.NORMAL]


def test_get_snapshots_defaults_to_normal_but_accepts_an_override(monkeypatch):
    calls = _spy_call_with_retry(monkeypatch)
    client = _client()

    class _FakeMarketData:
        def get_snapshot(self, symbols, category, extend_hour_required=False):
            return _FakeResponse([_REAL_SNAPSHOT_ROW])

    class _FakeDataClient:
        market_data = _FakeMarketData()

    client._require_data_client = lambda: _FakeDataClient()
    client.get_snapshots(["AAPL"])
    client.get_snapshots(["AAPL"], priority=retry_module.CallPriority.BACKGROUND)

    assert calls == [retry_module.CallPriority.NORMAL, retry_module.CallPriority.BACKGROUND]


def test_get_raw_bars_defaults_to_background_but_accepts_an_override(monkeypatch):
    calls = _spy_call_with_retry(monkeypatch)
    client = _client()

    class _FakeMarketData:
        def get_history_bar(self, symbol, category, timespan, count=None, trading_sessions=None):
            return _FakeResponse([])

    class _FakeDataClient:
        market_data = _FakeMarketData()

    client._require_data_client = lambda: _FakeDataClient()
    client.get_raw_bars("AAPL", "1d", 5)
    client.get_raw_bars("AAPL", "1d", 5, priority=retry_module.CallPriority.CRITICAL)

    assert calls == [retry_module.CallPriority.BACKGROUND, retry_module.CallPriority.CRITICAL]


# -- list_open_orders (batched broker-bracket status polling) ---------------

_REAL_OPEN_ORDER_ROW = {
    "client_order_id": "26532938-0ecf-409f-a4c8-7a54f38aa415",
    "order_id": "UHHKDO5KJPCH6JQ8SAEAQJOFIB",
    "order_type": "LIMIT",
    "limit_price": "322.56",
    "total_quantity": "2",
    "status": "SUBMITTED",
    "symbol": "AAPL",
    "side": "SELL",
}


def test_order_from_open_order_dict_uses_total_quantity_not_quantity():
    # Confirmed live 2026-08-11: get_order_open's real row shape uses
    # total_quantity, NOT the plain `quantity` key get_order_detail/
    # place_order use elsewhere in this file.
    client = _client()
    order = client._order_from_open_order_dict(_REAL_OPEN_ORDER_ROW)
    assert order.quantity == 2.0
    assert order.limit_price == 322.56
    assert order.status == OrderStatus.SUBMITTED
    assert order.broker_order_id == "26532938-0ecf-409f-a4c8-7a54f38aa415"


def test_order_from_open_order_dict_falls_back_to_quantity_key():
    client = _client()
    order = client._order_from_open_order_dict({"status": "SUBMITTED", "quantity": "5"})
    assert order.quantity == 5.0


def test_list_open_orders_maps_every_row():
    client = _client()
    client.account_id = "test-account"

    class _FakeOrderV3:
        def get_order_open(self, account_id):
            return _FakeResponse([_REAL_OPEN_ORDER_ROW, {**_REAL_OPEN_ORDER_ROW, "client_order_id": "leg-2", "order_type": "STOP_LOSS", "stop_price": "304.13", "limit_price": None}])

    class _FakeTradeClient:
        order_v3 = _FakeOrderV3()

    client._require_trade_client = lambda: _FakeTradeClient()
    orders = client.list_open_orders()

    assert len(orders) == 2
    assert {o.broker_order_id for o in orders} == {"26532938-0ecf-409f-a4c8-7a54f38aa415", "leg-2"}


def test_list_open_orders_uses_normal_priority_by_default(monkeypatch):
    calls = _spy_call_with_retry(monkeypatch)
    client = _client()
    client.account_id = "test-account"

    class _FakeOrderV3:
        def get_order_open(self, account_id):
            return _FakeResponse([])

    class _FakeTradeClient:
        order_v3 = _FakeOrderV3()

    client._require_trade_client = lambda: _FakeTradeClient()
    client.list_open_orders()

    assert calls == [retry_module.CallPriority.NORMAL]


# -- streaming (subscribe_quotes / _snapshot_from_streamed_result) ----------
# Confirmed live 2026-08-11 (scripts/verify_streaming.py --sub-type SNAPSHOT)
# against the real sandbox account -- field names below are read directly
# from the SDK's own snapshot_result.py, matching that live output.

class _FakeBasicResult:
    def __init__(self, symbol, timestamp, trading_session="RTH"):
        self.symbol = symbol
        self.timestamp = timestamp
        self.trading_session = trading_session


class _FakeSnapshotResult:
    def __init__(
        self, symbol, timestamp, price=None, open=None, high=None, low=None, pre_close=None,
        volume=None, ext_price=None, ext_high=None, ext_low=None, ext_volume=None, trading_session="RTH",
    ):
        self.basic = _FakeBasicResult(symbol, timestamp, trading_session)
        self.price = price
        self.open = open
        self.high = high
        self.low = low
        self.pre_close = pre_close
        self.volume = volume
        self.ext_price = ext_price
        self.ext_high = ext_high
        self.ext_low = ext_low
        self.ext_volume = ext_volume


def test_snapshot_from_streamed_result_maps_real_fields():
    client = _client()
    result = _FakeSnapshotResult(
        symbol="AAPL", timestamp=_REGULAR_HOURS_QUOTE_TIME_MS, price=304.39, open=307.75,
        high=309.97, low=302.79, pre_close=308.26, volume=22042211,
    )
    snapshot = client._snapshot_from_streamed_result(result)
    assert snapshot.symbol == "AAPL"
    assert snapshot.last_price == 304.39
    assert snapshot.open_price == 307.75
    assert snapshot.high_of_day == 309.97
    assert snapshot.low_of_day == 302.79
    assert snapshot.prev_close == 308.26
    assert snapshot.cumulative_volume == 22042211.0
    assert snapshot.vwap == snapshot.last_price  # not carried by this feed -- same fallback as REST
    assert snapshot.bid == 0.0 and snapshot.ask == 0.0  # not carried by this feed at all


def test_snapshot_from_streamed_result_ignores_ext_fields_during_regular_hours():
    client = _client()
    result = _FakeSnapshotResult(
        symbol="AAPL", timestamp=_REGULAR_HOURS_QUOTE_TIME_MS, price=304.39, volume=100,
        ext_price=999.0, ext_volume=999,
    )
    snapshot = client._snapshot_from_streamed_result(result)
    assert snapshot.last_price == 304.39
    assert snapshot.cumulative_volume == 100.0


def test_snapshot_from_streamed_result_honors_ext_fields_during_premarket():
    client = _client()
    result = _FakeSnapshotResult(
        symbol="AAPL", timestamp=_PREMARKET_QUOTE_TIME_MS, price=304.39, volume=100,
        ext_price=310.0, ext_volume=555, ext_high=311.0, ext_low=309.0,
    )
    snapshot = client._snapshot_from_streamed_result(result)
    assert snapshot.last_price == 310.0
    assert snapshot.cumulative_volume == 555.0
    assert snapshot.high_of_day == 311.0
    assert snapshot.low_of_day == 309.0


def test_snapshot_from_streamed_result_defaults_missing_fields_to_last_price():
    client = _client()
    result = _FakeSnapshotResult(symbol="AAPL", timestamp=_REGULAR_HOURS_QUOTE_TIME_MS, price=304.39)
    snapshot = client._snapshot_from_streamed_result(result)
    assert snapshot.high_of_day == 304.39
    assert snapshot.low_of_day == 304.39
    assert snapshot.open_price == 304.39
    assert snapshot.prev_close is None
    assert snapshot.cumulative_volume == 0.0


class _FakeAskBidResult:
    def __init__(self, price, size):
        self.price = price
        self.size = size


class _FakeQuoteResult:
    def __init__(self, symbol, timestamp, asks=None, bids=None, trading_session="RTH"):
        self.basic = _FakeBasicResult(symbol, timestamp, trading_session)
        self.asks = asks or []
        self.bids = bids or []


def test_quote_top_of_book_maps_real_fields():
    # Confirmed live 2026-08-11 (the very first verify_streaming.py run,
    # before SNAPSHOT was confirmed): asks:[price:304.22,size:203],
    # bids:[price:304.21,size:41].
    client = _client()
    result = _FakeQuoteResult(
        symbol="AAPL", timestamp=_REGULAR_HOURS_QUOTE_TIME_MS,
        asks=[_FakeAskBidResult(304.22, 203)], bids=[_FakeAskBidResult(304.21, 41)],
    )
    symbol, bid, ask, bid_size, ask_size = client._quote_top_of_book(result)
    assert symbol == "AAPL"
    assert bid == 304.21
    assert ask == 304.22
    assert bid_size == 41.0
    assert ask_size == 203.0


def test_quote_top_of_book_defaults_to_zero_when_a_side_is_empty():
    client = _client()
    result = _FakeQuoteResult(symbol="AAPL", timestamp=_REGULAR_HOURS_QUOTE_TIME_MS, asks=[], bids=[])
    symbol, bid, ask, bid_size, ask_size = client._quote_top_of_book(result)
    assert (bid, ask, bid_size, ask_size) == (0.0, 0.0, 0.0, 0.0)


def test_merge_streamed_snapshot_returns_none_until_both_message_types_seen():
    client = _streaming_client()

    assert client._merge_streamed_snapshot("AAPL") is None

    snapshot = MarketSnapshot(
        symbol="AAPL", timestamp=datetime.utcnow(), last_price=304.39, bid=0.0, ask=0.0,
        bid_size=0.0, ask_size=0.0, cumulative_volume=100.0, vwap=304.39, high_of_day=304.39,
        low_of_day=304.39, open_price=304.39,
    )
    client._streaming_snapshot_cache["AAPL"] = snapshot
    assert client._merge_streamed_snapshot("AAPL") is None  # still no quote data

    client._streaming_quote_cache["AAPL"] = (304.21, 304.22, 41.0, 203.0)
    merged = client._merge_streamed_snapshot("AAPL")
    assert merged is not None
    assert merged.last_price == 304.39  # from the snapshot cache
    assert merged.bid == 304.21 and merged.ask == 304.22  # from the quote cache
    assert merged.bid_size == 41.0 and merged.ask_size == 203.0


class _FakeTickResult:
    def __init__(self, symbol, timestamp, price=None, volume=None, side=None, trading_session="RTH"):
        self.basic = _FakeBasicResult(symbol, timestamp, trading_session)
        self.price = price
        self.volume = volume
        self.side = side


def test_tick_from_streamed_result_maps_known_side_codes():
    client = _client()
    result = _FakeTickResult(symbol="AAPL", timestamp=_REGULAR_HOURS_QUOTE_TIME_MS, price=304.39, volume=100, side="1")
    tick = client._tick_from_streamed_result(result)
    assert tick.symbol == "AAPL"
    assert tick.price == 304.39
    assert tick.volume == 100.0
    assert tick.side == TradeSide.BUY


def test_tick_from_streamed_result_falls_back_to_unknown_for_an_unrecognized_side():
    # See _TICK_SIDE_MAP's docstring: an unconfirmed/wrong guess must fail
    # safe (UNKNOWN), never silently misclassify a print as BUY or SELL.
    client = _client()
    result = _FakeTickResult(symbol="AAPL", timestamp=_REGULAR_HOURS_QUOTE_TIME_MS, price=304.39, volume=100, side="some-value-not-in-the-map")
    tick = client._tick_from_streamed_result(result)
    assert tick.side == TradeSide.UNKNOWN


def test_tick_from_streamed_result_defaults_missing_fields():
    client = _client()
    result = _FakeTickResult(symbol="AAPL", timestamp=_REGULAR_HOURS_QUOTE_TIME_MS)
    tick = client._tick_from_streamed_result(result)
    assert tick.price == 0.0
    assert tick.volume == 0.0
    assert tick.side == TradeSide.UNKNOWN


def test_get_recent_ticks_returns_empty_for_an_unsubscribed_symbol():
    client = _streaming_client()
    assert client.get_recent_ticks("AAPL") == []


def test_get_recent_ticks_returns_a_copy_not_the_live_buffer():
    client = _streaming_client()
    now = datetime.utcnow()
    tick = TickRecord(symbol="AAPL", timestamp=now, price=1.0, volume=10.0, side=TradeSide.BUY)
    client._streaming_tick_buffer["AAPL"] = deque([tick])

    returned = client.get_recent_ticks("AAPL")
    returned.append("mutated")

    assert list(client._streaming_tick_buffer["AAPL"]) == [tick]  # untouched by the caller's mutation


def test_get_recent_ticks_narrows_to_max_age_seconds():
    client = _streaming_client()
    now = datetime.utcnow()
    old_tick = TickRecord(symbol="AAPL", timestamp=now - timedelta(seconds=120), price=1.0, volume=10.0, side=TradeSide.BUY)
    fresh_tick = TickRecord(symbol="AAPL", timestamp=now - timedelta(seconds=5), price=1.0, volume=20.0, side=TradeSide.SELL)
    client._streaming_tick_buffer["AAPL"] = deque([old_tick, fresh_tick])

    assert client.get_recent_ticks("AAPL", max_age_seconds=60.0) == [fresh_tick]
    assert client.get_recent_ticks("AAPL") == [old_tick, fresh_tick]  # unnarrowed when max_age_seconds is None


class _FakeDataStreamingClient:
    """Stands in for webull.data.data_streaming_client.DataStreamingClient
    -- subscribe_quotes constructs one of these internally (not injectable
    via a normal argument), so tests monkeypatch the DataStreamingClient
    name inside the client module with this factory instead."""

    def __init__(self, app_key, app_secret, region, session_id, http_host=None, mqtt_host=None):
        self.app_key = app_key
        self.app_secret = app_secret
        self.region = region
        self.session_id = session_id
        self.http_host = http_host
        self.mqtt_host = mqtt_host
        self._on_connect_success = None
        self.on_quotes_message = None
        self.connect_calls = 0
        self.subscribe_calls = []
        self.unsubscribe_calls = []
        self.loop_stop_calls = 0
        self.disconnect_calls = 0
        self.raise_on_subscribe: Optional[Exception] = None
        self.raise_on_unsubscribe: Optional[Exception] = None
        # Mirrors the REAL SDK's own (misleadingly-named) behavior,
        # confirmed live 2026-08-11: get_connect_success() flips True the
        # instant on_connect_success is *assigned* -- no network activity
        # required -- so it can never distinguish "still connecting" from
        # "actually connected." Kept this way deliberately so a test
        # relying on get_connect_success() for that distinction would fail
        # the same way it did in production, rather than passing on a
        # fake that's nicer-behaved than reality.
        self._connect_success_flag = False

    @property
    def on_connect_success(self):
        return self._on_connect_success

    @on_connect_success.setter
    def on_connect_success(self, func):
        self._connect_success_flag = True
        self._on_connect_success = func

    def connect_and_loop_start(self):
        self.connect_calls += 1

    def get_connect_success(self):
        return self._connect_success_flag

    def subscribe(self, symbols, category, sub_types):
        self.subscribe_calls.append((list(symbols), category, list(sub_types)))
        if self.raise_on_subscribe is not None:
            raise self.raise_on_subscribe

    def unsubscribe(self, symbols, category, sub_types):
        self.unsubscribe_calls.append((list(symbols), category, list(sub_types)))
        if self.raise_on_unsubscribe is not None:
            raise self.raise_on_unsubscribe

    def loop_stop(self):
        self.loop_stop_calls += 1

    def disconnect(self):
        self.disconnect_calls += 1


def _streaming_client(trading_mode=None):
    from webull_bot.config import TradingMode

    client = _client()
    client.settings = SimpleNamespace(
        trading_mode=trading_mode or TradingMode.SANDBOX,
        webull=SimpleNamespace(app_key="test-key", app_secret="test-secret", base_url="api.sandbox.webull.com"),
    )
    client._streaming_client = None
    client._streaming_connected = False
    client._streaming_connect_attempted_at = None
    client._streaming_subscribed_symbols = set()
    client._streaming_snapshot_cache = {}
    client._streaming_quote_cache = {}
    client._streaming_tick_buffer = {}
    import threading
    client._streaming_lock = threading.Lock()
    return client


def _patch_streaming_client_factory(monkeypatch):
    from webull_bot.brokers.webull import client as client_module

    instances = []

    def _factory(*args, **kwargs):
        instance = _FakeDataStreamingClient(*args, **kwargs)
        instances.append(instance)
        return instance

    monkeypatch.setattr(client_module, "DataStreamingClient", _factory)
    return instances


def test_subscribe_quotes_creates_a_streaming_connection_on_first_call(monkeypatch):
    instances = _patch_streaming_client_factory(monkeypatch)
    client = _streaming_client()

    client.subscribe_quotes(["AAPL"], lambda snap: None)

    assert len(instances) == 1
    fake = instances[0]
    assert fake.connect_calls == 1
    assert fake.on_connect_success is not None
    assert fake.on_quotes_message is not None


def test_subscribe_quotes_uses_the_confirmed_sandbox_host(monkeypatch):
    from webull_bot.config import TradingMode

    instances = _patch_streaming_client_factory(monkeypatch)
    client = _streaming_client(trading_mode=TradingMode.SANDBOX)

    client.subscribe_quotes(["AAPL"], lambda snap: None)

    assert instances[0].mqtt_host == "data-api.sandbox.webull.com"


def test_subscribe_quotes_lets_the_sdk_auto_resolve_the_host_when_live(monkeypatch):
    from webull_bot.config import TradingMode

    instances = _patch_streaming_client_factory(monkeypatch)
    client = _streaming_client(trading_mode=TradingMode.LIVE)

    client.subscribe_quotes(["AAPL"], lambda snap: None)

    assert instances[0].mqtt_host is None


def test_subscribe_quotes_subscribes_on_connect_success(monkeypatch):
    instances = _patch_streaming_client_factory(monkeypatch)
    client = _streaming_client()
    client.subscribe_quotes(["AAPL", "MSFT"], lambda snap: None)
    fake = instances[0]

    fake._connected = True
    fake.on_connect_success(fake, None, "session-1")

    assert fake.subscribe_calls == [(["AAPL", "MSFT"], "US_STOCK", ["QUOTE", "SNAPSHOT", "TICK"])]


def test_subscribe_quotes_chunks_a_large_symbol_list_on_connect(monkeypatch):
    # Real incident (2026-08-13): subscribe() rejects the WHOLE call with
    # TOO_MANY_SYMBOLS ("Maximum number of symbols: 100") once the symbol
    # list exceeds 100 -- subscribe_quotes used to send every symbol in
    # one uncapped call, so once the tracked candidate list grew past
    # 100, EVERY subscribe attempt failed wholesale, every single tick,
    # forever. Now chunked the same way get_snapshots already chunks its
    # own 100-symbol-capped REST endpoint.
    instances = _patch_streaming_client_factory(monkeypatch)
    client = _streaming_client()
    symbols = [f"SYM{i}" for i in range(250)]
    client.subscribe_quotes(symbols, lambda snap: None)
    fake = instances[0]

    fake._connected = True
    fake.on_connect_success(fake, None, "session-1")

    assert len(fake.subscribe_calls) == 3
    assert [len(call[0]) for call in fake.subscribe_calls] == [100, 100, 50]
    # _on_connect_success reads current_symbols as sorted(self.
    # _streaming_subscribed_symbols) (a set), not insertion order.
    assert sum((call[0] for call in fake.subscribe_calls), []) == sorted(symbols)


def test_subscribe_quotes_chunking_keeps_going_after_one_batch_fails(monkeypatch):
    # Best-effort per chunk -- one bad batch shouldn't take down every
    # other symbol in the same call.
    instances = _patch_streaming_client_factory(monkeypatch)
    client = _streaming_client()
    symbols = [f"SYM{i}" for i in range(250)]
    client.subscribe_quotes(symbols, lambda snap: None)
    fake = instances[0]
    fake._connected = True

    original_subscribe = fake.subscribe
    calls = []

    def _flaky_subscribe(chunk, category, sub_types):
        calls.append(list(chunk))
        if len(calls) == 2:
            raise RuntimeError("simulated TOO_MANY_SYMBOLS-adjacent failure")
        return original_subscribe(chunk, category, sub_types)

    monkeypatch.setattr(fake, "subscribe", _flaky_subscribe)

    fake.on_connect_success(fake, None, "session-1")  # exception is caught and logged, not raised

    assert len(calls) == 3  # all three chunks were still attempted


def test_subscribe_quotes_delivers_a_merged_message_once_both_types_seen(monkeypatch):
    instances = _patch_streaming_client_factory(monkeypatch)
    client = _streaming_client()
    received = []
    client.subscribe_quotes(["AAPL"], received.append)
    fake = instances[0]

    snapshot_result = _FakeSnapshotResult(symbol="AAPL", timestamp=_REGULAR_HOURS_QUOTE_TIME_MS, price=304.39, volume=100)
    fake.on_quotes_message(fake, "snapshot", snapshot_result)
    assert received == []  # no quote data cached yet -- must not fire with a fabricated bid/ask

    quote_result = _FakeQuoteResult(
        symbol="AAPL", timestamp=_REGULAR_HOURS_QUOTE_TIME_MS,
        asks=[_FakeAskBidResult(304.40, 100)], bids=[_FakeAskBidResult(304.38, 50)],
    )
    fake.on_quotes_message(fake, "quote", quote_result)

    assert len(received) == 1
    assert received[0].symbol == "AAPL"
    assert received[0].last_price == 304.39  # from the snapshot message
    assert received[0].bid == 304.38 and received[0].ask == 304.40  # from the quote message

    # A second snapshot tick re-merges with the still-cached quote data,
    # firing again immediately -- doesn't wait for a fresh quote too.
    fake.on_quotes_message(fake, "snapshot", _FakeSnapshotResult(
        symbol="AAPL", timestamp=_REGULAR_HOURS_QUOTE_TIME_MS, price=305.00, volume=150,
    ))
    assert len(received) == 2
    assert received[1].last_price == 305.00
    assert received[1].bid == 304.38 and received[1].ask == 304.40


def test_subscribe_quotes_ignores_unknown_topics(monkeypatch):
    instances = _patch_streaming_client_factory(monkeypatch)
    client = _streaming_client()
    received = []
    client.subscribe_quotes(["AAPL"], received.append)
    fake = instances[0]

    # "tick" is now a real, handled topic (see the TICK tests below) -- use
    # something genuinely unrecognized to exercise the fallthrough branch.
    fake.on_quotes_message(fake, "event-quote", object())

    assert received == []


def test_subscribe_quotes_routes_tick_messages_into_the_buffer_not_on_update(monkeypatch):
    instances = _patch_streaming_client_factory(monkeypatch)
    client = _streaming_client()
    received = []
    client.subscribe_quotes(["AAPL"], received.append)
    fake = instances[0]

    # Uses a timestamp close to "now" (unlike _REGULAR_HOURS_QUOTE_TIME_MS,
    # a fixed historical fixture used elsewhere) -- the tick buffer prunes
    # against real datetime.utcnow() on every append (see
    # _TICK_BUFFER_MAX_AGE_SECONDS), so a stale fixed timestamp would be
    # evicted the instant it's added.
    recent_ms = int(datetime.utcnow().timestamp() * 1000)
    tick_result = _FakeTickResult(symbol="AAPL", timestamp=recent_ms, price=304.39, volume=100, side="1")
    fake.on_quotes_message(fake, "tick", tick_result)

    assert received == []  # ticks never feed the merged-snapshot on_update callback
    ticks = client.get_recent_ticks("AAPL")
    assert len(ticks) == 1
    assert ticks[0].price == 304.39
    assert ticks[0].volume == 100.0
    assert ticks[0].side == TradeSide.BUY


def test_subscribe_quotes_swallows_a_malformed_tick_payload(monkeypatch):
    instances = _patch_streaming_client_factory(monkeypatch)
    client = _streaming_client()
    client.subscribe_quotes(["AAPL"], lambda snap: None)
    fake = instances[0]

    fake.on_quotes_message(fake, "tick", object())  # missing .basic/.price/etc -- must not raise

    assert client.get_recent_ticks("AAPL") == []


def test_subscribe_quotes_swallows_an_on_update_exception(monkeypatch):
    instances = _patch_streaming_client_factory(monkeypatch)
    client = _streaming_client()

    def _boom(snap):
        raise RuntimeError("simulated callback failure")

    client.subscribe_quotes(["AAPL"], _boom)
    fake = instances[0]
    quote_result = _FakeQuoteResult(
        symbol="AAPL", timestamp=_REGULAR_HOURS_QUOTE_TIME_MS,
        asks=[_FakeAskBidResult(1.01, 10)], bids=[_FakeAskBidResult(1.00, 10)],
    )
    fake.on_quotes_message(fake, "quote", quote_result)  # cached only -- no merge yet, nothing to swallow
    result = _FakeSnapshotResult(symbol="AAPL", timestamp=_REGULAR_HOURS_QUOTE_TIME_MS, price=1.0)

    fake.on_quotes_message(fake, "snapshot", result)  # triggers the merge + on_update -- must not raise


def test_subscribe_quotes_reuses_the_existing_connection_for_new_symbols(monkeypatch):
    instances = _patch_streaming_client_factory(monkeypatch)
    client = _streaming_client()
    client.subscribe_quotes(["AAPL"], lambda snap: None)
    fake = instances[0]
    fake.on_connect_success(fake, None, "session-1")  # real MQTT handshake completes

    client.subscribe_quotes(["MSFT"], lambda snap: None)

    assert len(instances) == 1  # no second connection opened
    assert fake.connect_calls == 1
    # Only the NEW symbol -- not a re-subscribe of AAPL (already covered
    # by the on_connect_success call above).
    assert fake.subscribe_calls == [(["AAPL"], "US_STOCK", ["QUOTE", "SNAPSHOT", "TICK"]), (["MSFT"], "US_STOCK", ["QUOTE", "SNAPSHOT", "TICK"])]


def test_subscribe_quotes_does_not_trust_the_sdks_get_connect_success(monkeypatch):
    # Regression test for a real production bug (2026-08-11): the SDK's
    # own get_connect_success() flips True the instant on_connect_success
    # is assigned, not once the MQTT handshake actually finishes -- see
    # _FakeDataStreamingClient's docstring/property, which mirrors that
    # real quirk exactly. If subscribe_quotes ever goes back to trusting
    # it (instead of self._streaming_connected, set only from inside our
    # own _on_connect_success wrapper), this must fail.
    instances = _patch_streaming_client_factory(monkeypatch)
    client = _streaming_client()
    client.subscribe_quotes(["AAPL"], lambda snap: None)
    fake = instances[0]
    assert fake.get_connect_success() is True  # the SDK's misleading flag -- already "true"
    assert client._streaming_connected is False  # our own tracked flag -- correctly still false

    client.subscribe_quotes(["MSFT"], lambda snap: None)  # must NOT be sent directly yet

    assert fake.subscribe_calls == []  # still waiting for the real connect callback


def test_subscribe_quotes_skips_already_subscribed_symbols(monkeypatch):
    instances = _patch_streaming_client_factory(monkeypatch)
    client = _streaming_client()
    client.subscribe_quotes(["AAPL"], lambda snap: None)
    fake = instances[0]
    fake.on_connect_success(fake, None, "session-1")
    fake.subscribe_calls.clear()

    client.subscribe_quotes(["AAPL"], lambda snap: None)  # already subscribed -- no-op

    assert fake.subscribe_calls == []


def test_subscribe_quotes_defers_new_symbols_until_connected(monkeypatch):
    # Second call arrives before the first connection has actually
    # finished connecting (client._streaming_connected still False) -- the
    # new symbol must not be dropped, just picked up once
    # _on_connect_success genuinely fires and reads the full (by-then-
    # updated) subscribed-symbols set.
    instances = _patch_streaming_client_factory(monkeypatch)
    client = _streaming_client()
    client.subscribe_quotes(["AAPL"], lambda snap: None)
    assert client._streaming_connected is False

    client.subscribe_quotes(["MSFT"], lambda snap: None)
    fake = instances[0]
    assert fake.subscribe_calls == []  # not connected yet -- nothing sent directly

    fake.on_connect_success(fake, None, "session-1")

    assert client._streaming_connected is True
    assert fake.subscribe_calls == [(["AAPL", "MSFT"], "US_STOCK", ["QUOTE", "SNAPSHOT", "TICK"])]


def test_subscribe_quotes_propagates_a_failed_additional_subscribe(monkeypatch):
    # A subscribe REST call failing while already connected must not be
    # swallowed here -- see subscribe_quotes' docstring. Swallowing it
    # used to mean TradingLoop believed these symbols were subscribed
    # (nothing told it otherwise) even though they were never actually
    # registered, so they'd silently never stream for the rest of the
    # process. Propagating lets _ensure_streaming_subscribed's own
    # try/except catch it and skip adding these symbols to
    # self._streaming_requested_symbols, so they're retried next tick.
    instances = _patch_streaming_client_factory(monkeypatch)
    client = _streaming_client()
    client.subscribe_quotes(["AAPL"], lambda snap: None)
    fake = instances[0]
    fake.on_connect_success(fake, None, "session-1")
    fake.raise_on_subscribe = RuntimeError("simulated REST failure")

    with pytest.raises(RuntimeError):
        client.subscribe_quotes(["MSFT"], lambda snap: None)


def test_subscribe_quotes_retries_a_symbol_after_a_failed_subscribe(monkeypatch):
    # Real bug fixed 2026-08-12: a failed subscribe used to still mark the
    # symbol as subscribed (that bookkeeping happened before the call, not
    # after), so a second call with the SAME symbol recomputed it as
    # "already subscribed" and returned immediately -- no exception, no
    # actual subscribe attempt, silently never retried for the rest of the
    # process. Proves a second call now genuinely retries: it reaches the
    # broker's subscribe() a second time and succeeds once the transient
    # failure clears, rather than silently no-op'ing.
    instances = _patch_streaming_client_factory(monkeypatch)
    client = _streaming_client()
    client.subscribe_quotes(["AAPL"], lambda snap: None)
    fake = instances[0]
    fake.on_connect_success(fake, None, "session-1")
    fake.raise_on_subscribe = RuntimeError("simulated transient failure")

    with pytest.raises(RuntimeError):
        client.subscribe_quotes(["MSFT"], lambda snap: None)
    assert "MSFT" not in client._streaming_subscribed_symbols

    fake.raise_on_subscribe = None  # the transient failure clears
    client.subscribe_quotes(["MSFT"], lambda snap: None)  # must not be a silent no-op

    msft_calls = [call for call in fake.subscribe_calls if call[0] == ["MSFT"]]
    assert len(msft_calls) == 2  # the failed attempt AND the real retry
    assert "MSFT" in client._streaming_subscribed_symbols


def _fake_utcnow_module(monkeypatch, start):
    from webull_bot.brokers.webull import client as client_module

    box = {"now": start}

    class _FakeDateTime(datetime):
        @classmethod
        def utcnow(cls):
            return box["now"]

    monkeypatch.setattr(client_module, "datetime", _FakeDateTime)
    return box


def test_subscribe_quotes_reconnects_with_a_fresh_client_after_the_delay(monkeypatch):
    from webull_bot.brokers.webull.client import _STREAMING_RECONNECT_DELAY_SECONDS

    start = datetime(2026, 8, 11, 12, 0, 0)
    clock = _fake_utcnow_module(monkeypatch, start)
    instances = _patch_streaming_client_factory(monkeypatch)
    client = _streaming_client()

    client.subscribe_quotes(["AAPL"], lambda snap: None)  # creates the first (never-connecting) client
    stale_fake = instances[0]
    assert client._streaming_connected is False

    clock["now"] = start + timedelta(seconds=_STREAMING_RECONNECT_DELAY_SECONDS + 1)
    client.subscribe_quotes(["MSFT"], lambda snap: None)

    assert stale_fake.loop_stop_calls == 1  # the dead connection was torn down
    assert stale_fake.disconnect_calls == 1
    assert len(instances) == 2  # a fresh client was created instead of waiting forever
    fresh_fake = instances[1]
    assert fresh_fake.connect_calls == 1

    # Every symbol ever requested (not just the newest one) must be
    # resubscribed once the fresh connection comes up -- AAPL was never
    # actually confirmed subscribed against the dead connection.
    fresh_fake.on_connect_success(fresh_fake, None, "session-2")
    assert fresh_fake.subscribe_calls == [(["AAPL", "MSFT"], "US_STOCK", ["QUOTE", "SNAPSHOT", "TICK"])]


def test_subscribe_quotes_does_not_reconnect_before_the_delay_elapses(monkeypatch):
    from webull_bot.brokers.webull.client import _STREAMING_RECONNECT_DELAY_SECONDS

    start = datetime(2026, 8, 11, 12, 0, 0)
    clock = _fake_utcnow_module(monkeypatch, start)
    instances = _patch_streaming_client_factory(monkeypatch)
    client = _streaming_client()

    client.subscribe_quotes(["AAPL"], lambda snap: None)
    stale_fake = instances[0]

    clock["now"] = start + timedelta(seconds=_STREAMING_RECONNECT_DELAY_SECONDS - 1)
    client.subscribe_quotes(["MSFT"], lambda snap: None)

    assert stale_fake.loop_stop_calls == 0
    assert stale_fake.disconnect_calls == 0
    assert len(instances) == 1  # still waiting on the original connection
    assert stale_fake.subscribe_calls == []


# -- unsubscribe_quotes (2026-08-14, streaming-subscription-budget fix) ----
# See TradingLoopConfig.streaming_subscription_budget's docstring for the
# real incident this exists to fix: subscribe_quotes only ever grew
# self._streaming_subscribed_symbols, with no way to release a symbol, so
# once total tracked candidates passed Webull's real cumulative-per-
# session subscribe cap, every later subscribe failed wholesale forever.

def test_unsubscribe_quotes_releases_a_connected_symbol(monkeypatch):
    instances = _patch_streaming_client_factory(monkeypatch)
    client = _streaming_client()
    client.subscribe_quotes(["AAPL", "MSFT"], lambda snap: None)
    fake = instances[0]
    fake.on_connect_success(fake, None, "session-1")
    client._streaming_snapshot_cache["AAPL"] = "snapshot-placeholder"
    client._streaming_quote_cache["AAPL"] = (1.0, 1.1, 10, 10)

    client.unsubscribe_quotes(["AAPL"])

    assert fake.unsubscribe_calls == [(["AAPL"], "US_STOCK", ["QUOTE", "SNAPSHOT", "TICK"])]
    assert client._streaming_subscribed_symbols == {"MSFT"}
    assert "AAPL" not in client._streaming_snapshot_cache
    assert "AAPL" not in client._streaming_quote_cache


def test_unsubscribe_quotes_is_a_noop_for_a_symbol_never_subscribed(monkeypatch):
    instances = _patch_streaming_client_factory(monkeypatch)
    client = _streaming_client()
    client.subscribe_quotes(["AAPL"], lambda snap: None)
    fake = instances[0]
    fake.on_connect_success(fake, None, "session-1")

    client.unsubscribe_quotes(["NEVER-SUBSCRIBED"])

    assert fake.unsubscribe_calls == []
    assert client._streaming_subscribed_symbols == {"AAPL"}


def test_unsubscribe_quotes_skips_the_real_call_when_not_yet_connected(monkeypatch):
    # A symbol optimistically recorded as subscribed while the connection
    # is still coming up (see subscribe_quotes' docstring) has no real
    # server-side subscription to release yet -- only local bookkeeping
    # needs to be dropped.
    instances = _patch_streaming_client_factory(monkeypatch)
    client = _streaming_client()
    client.subscribe_quotes(["AAPL"], lambda snap: None)
    fake = instances[0]
    assert client._streaming_connected is False

    client.unsubscribe_quotes(["AAPL"])

    assert fake.unsubscribe_calls == []
    assert client._streaming_subscribed_symbols == set()


def test_unsubscribe_quotes_chunks_a_large_symbol_list(monkeypatch):
    instances = _patch_streaming_client_factory(monkeypatch)
    client = _streaming_client()
    many_symbols = [f"SYM{i}" for i in range(150)]
    client.subscribe_quotes(many_symbols, lambda snap: None)
    fake = instances[0]
    fake.on_connect_success(fake, None, "session-1")
    fake.unsubscribe_calls.clear()

    client.unsubscribe_quotes(many_symbols)

    assert len(fake.unsubscribe_calls) == 2  # chunked at 100, same as subscribe
    assert sum(len(call[0]) for call in fake.unsubscribe_calls) == 150
    assert client._streaming_subscribed_symbols == set()


def test_unsubscribe_quotes_drops_local_bookkeeping_even_when_the_broker_call_fails(monkeypatch):
    instances = _patch_streaming_client_factory(monkeypatch)
    client = _streaming_client()
    client.subscribe_quotes(["AAPL"], lambda snap: None)
    fake = instances[0]
    fake.on_connect_success(fake, None, "session-1")
    fake.raise_on_unsubscribe = RuntimeError("simulated unsubscribe failure")

    client.unsubscribe_quotes(["AAPL"])  # must not raise

    assert client._streaming_subscribed_symbols == set()


def test_disconnect_stops_the_streaming_client():
    client = _streaming_client()

    class _FakeStreamingClient:
        def __init__(self):
            self.loop_stop_called = False
            self.disconnect_called = False

        def loop_stop(self):
            self.loop_stop_called = True

        def disconnect(self):
            self.disconnect_called = True

    fake = _FakeStreamingClient()
    client._streaming_client = fake
    client._streaming_subscribed_symbols = {"AAPL"}
    client._streaming_snapshot_cache = {"AAPL": object()}
    client._streaming_quote_cache = {"AAPL": (1.0, 1.01, 10.0, 10.0)}

    client.disconnect()

    assert fake.loop_stop_called is True
    assert fake.disconnect_called is True
    assert client._streaming_client is None
    assert client._streaming_subscribed_symbols == set()
    assert client._streaming_snapshot_cache == {}
    assert client._streaming_quote_cache == {}
