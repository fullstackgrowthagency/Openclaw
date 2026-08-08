"""
Webull OpenAPI broker client, built on the official `webull-openapi-python-sdk`
(package `webull`, PyPI: webull-openapi-python-sdk).

Verified live against the sandbox host (api.sandbox.webull.com) on 2026-08-08:
  - Auth: ApiClient(app_key, app_secret, "us") + add_endpoint("us", host);
    TradeClient(api_client) / DataClient(api_client) both call an internal
    /openapi/config + token-check handshake on construction. For the
    sandbox app used to verify this, token_check_enabled is False, so
    construction returns immediately with no 2FA wait. If a different app
    has 2FA/token-check enabled, TradeClient(...)/DataClient(...) will block
    for up to `token_check_duration_seconds` (default 300s) polling for a
    human to approve a push notification in the Webull app -- see
    webull.core.http.initializer.token.token_manager for the exact flow.
  - account_v2.get_account_balance(account_id) -> confirmed field names
    used below (total_net_liquidation_value, account_currency_assets[0].*).
  - account_v2.get_account_position(account_id) -> confirmed shape is a
    JSON list, but only verified EMPTY (no open positions existed during
    verification). Field names in _position_from_dict are a best-effort
    guess at Webull's snake_case convention (matches every other verified
    endpoint) and MUST be re-checked against a real populated response
    before being trusted -- see the TODO in _position_from_dict.
  - order_v3.place_order(account_id, [order_dict]) -> confirmed the request
    schema (instrument_type must be "EQUITY", not the SDK's internal
    InstrumentType.STOCK enum name -- that enum is not what this endpoint
    expects). Successful-response body shape is UNVERIFIED: every live test
    was correctly rejected with OAUTH_OPENAPI_CAN_NOT_TRADING_FOR_NON_TRADING_HOURS
    because verification happened on a weekend. Re-verify _order_from_response
    the next time an order is actually placed during market hours.
  - order_v3.cancel_order(account_id, client_order_id),
    get_order_detail(account_id, client_order_id) -> confirmed to accept
    the client-generated client_order_id as the lookup key (no separate
    server-assigned order id needed), which is why `Order.broker_order_id`
    is set to the same value as `client_order_id` here.
  - market_data.get_snapshot([symbol], "US_STOCK") and
    get_history_bar(symbol, "US_STOCK", "M1", count=...) -> confirmed field
    names used in _snapshot_from_dict / _snapshots_from_bars. Note: neither
    endpoint returns VWAP directly -- see the comment in _snapshot_from_dict.
  - Streaming (DataStreamingClient, MQTT-based) is NOT implemented here.
    Its constructor needs an http_host/mqtt_host; the production values are
    documented (data-api.webull.com) but no sandbox equivalent was found or
    confirmed live. Wire this up once that host is confirmed, rather than
    guessing it.

Safety (unchanged from the skeleton this replaces):
  - `WebullBrokerClient.is_live` reflects the *configured* trading mode.
  - The constructor calls `settings.require_non_live_or_authorized()`, so a
    live-mode instance cannot even be constructed unless
    `Settings.is_live_trading_authorized()` returns True.
  - `OrderManager` (execution/order_manager.py) additionally re-checks
    authorization before every single order, so a mode flip mid-process
    can't slip an order through.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from webull.core.client import ApiClient
from webull.data.common.category import Category
from webull.data.common.timespan import Timespan
from webull.data.data_client import DataClient
from webull.trade.trade_client import TradeClient

from ...config import Settings, TradingMode
from ...enums import OrderSide, OrderStatus, OrderType, TimeInForce
from ...interfaces.broker import BrokerClient
from ...models import Fill, MarketSnapshot, Order, Position

logger = logging.getLogger(__name__)

_REGION = "us"  # only region verified/needed for this project (US equities)

_INTERVAL_TO_TIMESPAN = {
    "1m": Timespan.M1.name,
    "5m": Timespan.M5.name,
    "15m": Timespan.M15.name,
    "30m": Timespan.M30.name,
    "1h": Timespan.M60.name,
    "1d": Timespan.D.name,
}

_SIDE_TO_WEBULL = {
    OrderSide.BUY: "BUY",
    OrderSide.SELL: "SELL",
    OrderSide.SELL_SHORT: "SHORT",
    # Webull has no distinct "cover" side -- covering a short is a plain BUY.
    OrderSide.BUY_TO_COVER: "BUY",
}

_ORDER_TYPE_TO_WEBULL = {
    OrderType.MARKET: "MARKET",
    OrderType.LIMIT: "LIMIT",
    # Webull's plain stop order is named STOP_LOSS/STOP_LOSS_LIMIT internally
    # regardless of order direction -- confirmed via webull.trade.common.order_type.
    OrderType.STOP: "STOP_LOSS",
    OrderType.STOP_LIMIT: "STOP_LOSS_LIMIT",
}

_TIF_TO_WEBULL = {
    TimeInForce.DAY: "DAY",
    TimeInForce.GTC: "GTC",
    TimeInForce.IOC: "IOC",
    # Webull's SDK enum (order_tif.py) has no FOK member -- confirmed, not an oversight.
}

_WEBULL_STATUS_TO_OURS = {
    "SUBMITTED": OrderStatus.SUBMITTED,
    "CANCELLED": OrderStatus.CANCELED,
    "FAILED": OrderStatus.REJECTED,
    "FILLED": OrderStatus.FILLED,
    "PARTIAL_FILLED": OrderStatus.PARTIALLY_FILLED,
}


def _epoch_ms_to_dt(ms: Optional[int]) -> datetime:
    if not ms:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).replace(tzinfo=None)


class WebullBrokerClient(BrokerClient):
    def __init__(self, settings: Settings):
        settings.require_non_live_or_authorized()
        if not settings.webull.is_configured():
            raise RuntimeError(
                "Webull credentials are not configured. Set WEBULL_APP_KEY, "
                "WEBULL_APP_SECRET, WEBULL_ACCOUNT_ID and WEBULL_BASE_URL "
                "(see .env.example)."
            )
        self.settings = settings
        self.account_id = settings.webull.account_id
        self._api_client: Optional[ApiClient] = None
        self._trade_client: Optional[TradeClient] = None
        self._data_client: Optional[DataClient] = None

    def connect(self) -> None:
        self._api_client = ApiClient(
            self.settings.webull.app_key, self.settings.webull.app_secret, _REGION
        )
        self._api_client.add_endpoint(_REGION, self.settings.webull.base_url)
        # NOTE: both of these hit the network (a /openapi/config + token-check
        # handshake) and can block for minutes if the app has 2FA/token-check
        # enabled and no human approves the resulting push notification in
        # time -- see this module's docstring.
        self._trade_client = TradeClient(self._api_client)
        self._data_client = DataClient(self._api_client)

    def disconnect(self) -> None:
        self._api_client = None
        self._trade_client = None
        self._data_client = None

    def _require_trade_client(self) -> TradeClient:
        if self._trade_client is None:
            raise RuntimeError("WebullBrokerClient.connect() must be called first")
        return self._trade_client

    def _require_data_client(self) -> DataClient:
        if self._data_client is None:
            raise RuntimeError("WebullBrokerClient.connect() must be called first")
        return self._data_client

    # -- account -----------------------------------------------------------

    def _get_primary_currency_asset(self) -> dict:
        response = self._require_trade_client().account_v2.get_account_balance(self.account_id)
        response.raise_for_status()
        body = response.json()
        assets = body.get("account_currency_assets") or []
        if not assets:
            raise RuntimeError(f"Webull returned no account_currency_assets for account {self.account_id}")
        return assets[0]

    def get_account_equity(self) -> float:
        return float(self._get_primary_currency_asset()["net_liquidation_value"])

    def get_buying_power(self) -> float:
        return float(self._get_primary_currency_asset()["buying_power"])

    def _position_from_dict(self, raw: dict) -> Position:
        # TODO: field names below are a best-effort guess following Webull's
        # snake_case convention seen on every other verified endpoint --
        # re-verify against a real populated get_account_position() response
        # (none existed during integration; the sandbox account had zero
        # positions) and correct any mismatches.
        side_raw = raw.get("side", "BUY")
        side = OrderSide.SELL_SHORT if side_raw == "SHORT" else OrderSide.BUY
        return Position(
            symbol=raw["symbol"],
            side=side,
            quantity=float(raw.get("quantity", raw.get("qty", 0))),
            avg_entry_price=float(raw.get("cost_price", raw.get("avg_cost", 0))),
            stop_price=None,
            target_price=None,
            trailing_stop_pct=None,
            opened_at=_epoch_ms_to_dt(raw.get("open_time")),
            strategy_name="unknown",
            realized_pnl=0.0,
        )

    def get_positions(self) -> list[Position]:
        response = self._require_trade_client().account_v2.get_account_position(self.account_id)
        response.raise_for_status()
        return [self._position_from_dict(row) for row in response.json()]

    # -- market data ---------------------------------------------------------

    def _snapshot_from_dict(self, raw: dict, timestamp: Optional[datetime] = None) -> MarketSnapshot:
        last_price = float(raw["price"])
        # Webull's snapshot endpoint does not return VWAP directly (confirmed
        # live -- the field is simply absent). Falling back to last_price
        # keeps distance_from_vwap_pct at a neutral 0 rather than fabricating
        # a biased number; replace once a real VWAP source is wired up
        # (e.g. accumulating price*volume from get_history_bar ourselves).
        vwap = last_price
        return MarketSnapshot(
            symbol=raw.get("symbol", ""),
            timestamp=timestamp or _epoch_ms_to_dt(raw.get("quote_time")),
            last_price=last_price,
            bid=float(raw.get("bid", 0) or 0),
            ask=float(raw.get("ask", 0) or 0),
            bid_size=float(raw.get("bid_size", 0) or 0),
            ask_size=float(raw.get("ask_size", 0) or 0),
            cumulative_volume=float(raw.get("volume", 0) or 0),
            vwap=vwap,
            high_of_day=float(raw.get("high", last_price)),
            low_of_day=float(raw.get("low", last_price)),
            open_price=float(raw.get("open", last_price)),
            premarket_high=None,  # not returned by this endpoint
            prev_close=float(raw["pre_close"]) if raw.get("pre_close") is not None else None,
        )

    def get_snapshot(self, symbol: str) -> MarketSnapshot:
        response = self._require_data_client().market_data.get_snapshot([symbol], Category.US_STOCK.name)
        response.raise_for_status()
        rows = response.json()
        if not rows:
            raise ValueError(f"Webull returned no snapshot for {symbol}")
        return self._snapshot_from_dict(rows[0])

    def _snapshots_from_bars(self, symbol: str, raw_bars: list[dict]) -> list[MarketSnapshot]:
        # Webull returns bars most-recent-first; this project's interface
        # contract (see interfaces/broker.py) wants oldest-first.
        bars = sorted(raw_bars, key=lambda b: b["time"])
        snapshots: list[MarketSnapshot] = []
        cumulative_volume = 0.0
        cumulative_pv = 0.0
        running_high = float("-inf")
        running_low = float("inf")
        session_open = float(bars[0]["open"]) if bars else 0.0

        for bar in bars:
            close = float(bar["close"])
            high = float(bar["high"])
            low = float(bar["low"])
            volume = float(bar["volume"])

            cumulative_volume += volume
            cumulative_pv += close * volume
            running_high = max(running_high, high)
            running_low = min(running_low, low)
            # This is a running VWAP over the *fetched window*, not
            # necessarily the full trading session -- accurate only if the
            # `count`/lookback passed to get_bars covers the whole session
            # from open. Documented limitation, not a silent approximation.
            vwap = cumulative_pv / cumulative_volume if cumulative_volume else close

            timestamp = datetime.strptime(bar["time"], "%Y-%m-%dT%H:%M:%S.%f%z").replace(tzinfo=None)

            snapshots.append(
                MarketSnapshot(
                    symbol=symbol,
                    timestamp=timestamp,
                    last_price=close,
                    bid=0.0,
                    ask=0.0,
                    bid_size=0.0,
                    ask_size=0.0,
                    cumulative_volume=cumulative_volume,
                    vwap=vwap,
                    high_of_day=running_high,
                    low_of_day=running_low,
                    open_price=session_open,
                )
            )
        return snapshots

    def get_bars(self, symbol: str, interval: str, lookback: int) -> list[MarketSnapshot]:
        timespan = _INTERVAL_TO_TIMESPAN.get(interval)
        if timespan is None:
            raise ValueError(f"Unsupported interval {interval!r}; supported: {sorted(_INTERVAL_TO_TIMESPAN)}")
        response = self._require_data_client().market_data.get_history_bar(
            symbol, Category.US_STOCK.name, timespan, count=str(lookback)
        )
        response.raise_for_status()
        return self._snapshots_from_bars(symbol, response.json())

    def subscribe_quotes(self, symbols: list[str], on_update: Callable[[MarketSnapshot], None]) -> None:
        raise NotImplementedError(
            "Streaming (DataStreamingClient, MQTT-based) is not wired up: its "
            "constructor needs a confirmed sandbox mqtt_host, which was not "
            "found or verified live during integration (only the production "
            "host data-api.webull.com is documented). Confirm the sandbox "
            "equivalent before implementing this rather than guessing it. "
            "Poll get_snapshot()/get_bars() in the meantime."
        )

    # -- orders --------------------------------------------------------------

    def _order_payload(self, order: Order) -> dict:
        try:
            side = _SIDE_TO_WEBULL[order.side]
            order_type = _ORDER_TYPE_TO_WEBULL[order.order_type]
        except KeyError as exc:
            raise ValueError(f"Unsupported order field for Webull: {exc}") from exc
        try:
            tif = _TIF_TO_WEBULL[order.time_in_force]
        except KeyError as exc:
            raise ValueError(f"Webull has no equivalent time_in_force for {exc}") from exc

        client_order_id = order.client_order_id or str(uuid.uuid4())
        payload = {
            "combo_type": "NORMAL",
            "symbol": order.symbol,
            "instrument_type": "EQUITY",  # confirmed live; NOT webull.trade.common.instrument_type.STOCK
            "market": "US",
            "order_type": order_type,
            "quantity": str(order.quantity),
            "side": side,
            "time_in_force": tif,
            "support_trading_session": "CORE",
            "entrust_type": "QTY",
            "client_order_id": client_order_id,
        }
        if order.limit_price is not None:
            payload["limit_price"] = str(order.limit_price)
        if order.stop_price is not None:
            # UNVERIFIED field name -- current strategies only ever submit
            # MARKET orders (see execution/order_manager.py), so this path
            # has not been exercised live. Confirm before relying on it.
            payload["stop_price"] = str(order.stop_price)
        return payload

    def place_order(self, order: Order) -> Order:
        if self.is_live and not self.settings.is_live_trading_authorized():
            # Defense in depth: even though __init__ already checked this, a
            # long-lived process could have its settings reloaded/mutated.
            raise RuntimeError("Live trading authorization lost; refusing to place order.")

        payload = self._order_payload(order)
        response = self._require_trade_client().order_v3.place_order(self.account_id, [payload])
        response.raise_for_status()

        # Webull's own cancel/detail calls key off the client-generated
        # client_order_id (confirmed live), so there is no separate
        # server-assigned id to parse out of the response body here.
        order.client_order_id = payload["client_order_id"]
        order.broker_order_id = payload["client_order_id"]
        order.updated_at = datetime.utcnow()
        # A 2xx response means Webull accepted the order for processing, not
        # that it has filled -- the response body's success shape is
        # UNVERIFIED (see module docstring). Callers must poll
        # get_order_status()/poll_fills() for the authoritative state.
        order.status = OrderStatus.SUBMITTED
        return order

    def cancel_order(self, broker_order_id: str) -> None:
        response = self._require_trade_client().order_v3.cancel_order(self.account_id, broker_order_id)
        response.raise_for_status()

    def modify_order(self, broker_order_id: str, **changes) -> Order:
        # UNVERIFIED request/response shape -- no live order existed to test
        # a replace against during integration. modify_orders is confirmed
        # to be a list-of-dicts like place_order's new_orders (same
        # ReplaceOrderRequest.set_modify_orders pattern), but the exact
        # accepted keys beyond client_order_id were not confirmed live.
        modify_payload = {"client_order_id": broker_order_id, **changes}
        response = self._require_trade_client().order_v3.replace_order(self.account_id, [modify_payload])
        response.raise_for_status()
        return self.get_order_status(broker_order_id)

    def _order_from_detail(self, raw: dict) -> Order:
        # UNVERIFIED field names for a populated response -- see module
        # docstring (every live place_order attempt was correctly rejected
        # for being outside market hours, so no real order detail was ever
        # fetched). Re-check this mapping against a real filled/open order.
        status = _WEBULL_STATUS_TO_OURS.get(raw.get("status", ""), OrderStatus.PENDING)
        return Order(
            symbol=raw.get("symbol", ""),
            side=OrderSide.BUY if raw.get("side") == "BUY" else OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=float(raw.get("quantity", 0)),
            limit_price=float(raw["limit_price"]) if raw.get("limit_price") is not None else None,
            status=status,
            client_order_id=raw.get("client_order_id"),
            broker_order_id=raw.get("client_order_id"),
        )

    def get_order_status(self, broker_order_id: str) -> Order:
        response = self._require_trade_client().order_v3.get_order_detail(self.account_id, broker_order_id)
        response.raise_for_status()
        body = response.json()
        row = body[0] if isinstance(body, list) else body
        return self._order_from_detail(row)

    def poll_fills(self, since: Optional[datetime] = None) -> list[Fill]:
        # UNVERIFIED field names for a populated response -- no live
        # execution existed to test against during integration.
        start_date = since.strftime("%Y-%m-%d") if since else None
        response = self._require_trade_client().order_v3.get_order_executions(
            self.account_id, start_date=start_date
        )
        response.raise_for_status()
        fills = []
        for row in response.json():
            fills.append(
                Fill(
                    order_client_id=row.get("client_order_id", ""),
                    symbol=row.get("symbol", ""),
                    side=OrderSide.BUY if row.get("side") == "BUY" else OrderSide.SELL,
                    quantity=float(row.get("quantity", 0)),
                    price=float(row.get("price", 0)),
                    filled_at=_epoch_ms_to_dt(row.get("execution_time")),
                    fees=float(row.get("fees", 0) or 0),
                )
            )
        return fills

    @property
    def is_live(self) -> bool:
        return self.settings.trading_mode == TradingMode.LIVE
