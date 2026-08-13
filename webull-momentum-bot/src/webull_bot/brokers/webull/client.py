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
  - account_v2.get_account_position(account_id) -> confirmed live on
    2026-08-10 against a REAL populated response (3 open positions, a
    separate production incident -- see scripts/list_and_close_positions.py).
    Confirmed real row shape: {currency, quantity, cost, proportion,
    position_id, symbol, instrument_type, cost_price, last_price,
    market_value, unrealized_profit_loss, unrealized_profit_loss_rate,
    day_profit_loss, day_realized_profit_loss}. `_position_from_dict`'s
    guesses for `symbol`/`quantity`/`cost_price` were all correct. Two
    guesses were NOT: there is no `side` field at all (so a short position
    cannot actually be distinguished from a long by this response --
    _position_from_dict's `side_raw = raw.get("side", "BUY")` will always
    silently default to BUY; not fixed yet since every position observed so
    far has been long, and there's no confirmed alternate field to key off
    for direction) and no `open_time` field either (so `opened_at` is not
    the real fill time, whatever `_epoch_ms_to_dt(None)` returns instead).
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
    is set to the same value as `client_order_id` here. cancel_order
    specifically needs each COMBO LEG's own client_order_id -- confirmed
    live that a combo-level client_combo_order_id returns ORDER_NOT_FOUND.
  - order_v3.place_order(account_id, [stop_dict, target_dict]) with both
    dicts sharing one client_combo_order_id and combo_type="OCO" ->
    confirmed live (2026-08-11, scripts/verify_bracket_orders.py) that a
    2-leg OCO stop+target bracket can be attached to an ALREADY-OPEN
    position with no MASTER (entry) leg -- see place_oco_bracket. Also
    confirmed live the same session: modify_order/replace_order's effect
    on a resting order's price is inconclusive (call returned "accepted"
    but the immediate readback showed the field unset) -- breakeven/
    trailing-stop updates use cancel_order + place_oco_bracket/place_order
    again instead of relying on modify_order/replace_order.
  - market_data.get_snapshot([symbol], "US_STOCK") and
    get_history_bar(symbol, "US_STOCK", "M1", count=...) -> confirmed field
    names used in _snapshot_from_dict / _snapshots_from_bars. Note: neither
    endpoint returns VWAP directly -- see the comment in _snapshot_from_dict.
    NOT covered by this 2026-08-08 verification: get_snapshot is now called
    with extend_hour_required=True (added later) so pre-market/after-hours
    prices/volumes aren't silently stale or zero, and _snapshot_from_dict
    prefers `ext_price`/`ext_volume` fields when present AND the quote
    timestamp itself falls outside the regular 9:30am-4:00pm ET session
    (_is_outside_regular_session) -- the time gate exists because it's
    unconfirmed whether Webull actually zeroes those fields out once the
    regular session opens, versus them echoing that morning's last
    pre-market value all day; gating on wall-clock time avoids ever using a
    stale pre-market number during regular hours regardless of which way
    that turns out. Both the flag's effect on the REST body and the
    `ext_price`/`ext_volume` key names themselves are inferred from the
    SDK's protobuf streaming schema, not confirmed against a real sandbox
    response -- re-verify live during an actual pre-market/after-hours
    session before trusting the exact field names (see
    scripts/verify_extended_hours_bars.py for the analogous check already
    written for get_raw_bars' trading_sessions parameter).
  - Streaming (DataStreamingClient, MQTT-based) is NOT YET WIRED INTO THIS
    CLIENT, but is CONFIRMED LIVE AND WORKING as of 2026-08-11 (see
    scripts/verify_streaming.py). The SDK's own auto-resolution
    (`mqtt_host=None`) only knows `data-api.webull.com` -- the PRODUCTION
    host -- via its bundled `webull/core/data/endpoints.json`, which has
    no sandbox entry for quotes-api at all. Passing that resolved host
    explicitly against sandbox credentials connected over MQTT
    successfully but the subscribe REST call was rejected with `417
    INVALID_SESSION` ("Mqtt connection not exist for session") --
    confirmed NOT a timing race (identical result with a 2s delay added
    before subscribing) -- consistent with the MQTT session having been
    registered against the production quotes system while the subscribe
    REST call correctly targeted `api.sandbox.webull.com`, two systems
    with no shared session state. The correct sandbox host, confirmed live
    by explicitly passing it, is **`data-api.sandbox.webull.com`**
    (mirroring the already-known `api.webull.com` -> `api.sandbox.webull.com`
    REST split) -- real quote ticks (bid/ask price+size, `trading_session:
    RTH`) arrived within seconds of subscribing. `WebullBrokerClient`
    itself still doesn't implement `subscribe_quotes` (see that method) --
    this host is the missing piece integration was blocked on, not yet
    wired into production code.
  - Rate limiting: firing 10 truly concurrent get_snapshot calls at the
    sandbox tripped a 429 TOO_MANY_REQUESTS on 2 of them (2026-08-08), with
    no data corruption or cross-request mixups -- the SDK itself is safe to
    call from multiple threads. But that understated the real constraint:
    the sandbox enforces something close to a flat ~1 request/second
    sustained ceiling regardless of concurrency, and even a rate-limiter-
    paced retry loop can fail under concurrency if only the *first* attempt
    of each call is paced (retries firing on their own backoff schedule can
    still stack up across threads and re-trigger each other's 429s). Both
    get_snapshot and get_bars go through retry.call_with_retry, which now
    paces every attempt -- including retries -- through the shared
    retry.webull_market_data_limiter (see that module's docstring for the
    full discovery writeup, since it was a multi-step process to find the
    actual failure mode). This matters because BroadScanner calls
    get_snapshot concurrently across many symbols at once.
  - Priority-tiered rate limiting (2026-08-11): every Webull call this
    client makes -- not just market_data.* -- now goes through the same
    shared, priority-aware `retry.webull_limiter` (see retry.py's
    docstring for the CRITICAL/NORMAL/BACKGROUND tiers and which call
    sites use which). Previously order_v3.*/account_v2.* calls
    (place_order, cancel_order, get_order_status, get_positions,
    get_account_balance, ...) had NO rate-limit protection at all despite
    sharing this exact same account-wide budget -- a burst of market-data
    polling could 429 a stop-loss cancel or an entry-fill confirmation
    with nothing to retry it. Closing that gap, and letting exit-critical
    calls win contention over discovery/resistance-refresh traffic
    instead of queuing behind it in plain arrival order, was the whole
    point of adding priority in the first place.

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
import threading
import uuid
from dataclasses import replace as _dataclass_replace
from datetime import datetime, time, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from webull.core.client import ApiClient
from webull.data.common.category import Category
from webull.data.common.timespan import Timespan
from webull.data.data_client import DataClient
from webull.data.data_streaming_client import DataStreamingClient
from webull.trade.trade_client import TradeClient

from ...config import Settings, TradingMode
from ...enums import OrderSide, OrderStatus, OrderType, TimeInForce
from ...interfaces.broker import BrokerClient
from ...market_hours import is_within_core_trading_hours
from ...models import Fill, MarketSnapshot, Order, Position
from .retry import CallPriority, call_with_retry, webull_limiter

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

# get_snapshot's own docstring: "For each request, up to 100 symbols can be
# subscribed" -- used by get_snapshots to chunk a larger symbol list.
_SNAPSHOT_BATCH_SIZE = 100

# Confirmed live 2026-08-13: DataStreamingClient.subscribe() rejects the
# WHOLE call with TOO_MANY_SYMBOLS ("Maximum number of symbols: 100") once
# the symbol list passed to it exceeds 100 -- a real incident where
# subscribe_quotes was calling it with every not-yet-subscribed symbol at
# once, uncapped. Once the tracked candidate list grew past 100, EVERY
# subscribe attempt failed wholesale, repeated every single tick forever
# (caught and logged as a WARNING, so nothing crashed, but streaming
# silently never worked at all above 100 symbols, and the same failing
# call kept burning real time/log volume every cycle). A distinct
# constant from _SNAPSHOT_BATCH_SIZE above rather than reusing it: this is
# a different SDK endpoint (streaming subscribe vs. REST
# market_data.get_snapshot) whose limit currently happens to also be 100,
# not guaranteed to stay that way.
_STREAMING_SUBSCRIBE_BATCH_SIZE = 100

# Confirmed live 2026-08-11 (scripts/verify_streaming.py) -- the SDK's own
# bundled endpoints.json only knows the PRODUCTION quotes-api host
# (data-api.webull.com) for any region; there is no sandbox entry in it at
# all. This is the sandbox equivalent, found by testing the same
# api.webull.com -> api.sandbox.webull.com naming pattern the REST API
# already uses -- NOT documented anywhere the docstring above's citations
# were pulled from, purely a live-tested guess that happened to be right.
# See subscribe_quotes' docstring for how this is selected.
_SANDBOX_MQTT_STREAMING_HOST = "data-api.sandbox.webull.com"

# How long to wait for a pending streaming connection attempt to finish
# connecting (_on_connect_success firing) before giving up on it and
# retrying with a fresh DataStreamingClient -- see subscribe_quotes'
# docstring. Not itself live-tuned against a real stuck connection (all
# real connects observed so far have completed in well under this), just
# a conservative "clearly longer than a healthy connect should ever take"
# threshold -- short enough that a genuinely dead connection recovers
# within a couple of TradingLoop ticks, long enough not to abandon a
# connection attempt that's merely a bit slow.
_STREAMING_RECONNECT_DELAY_SECONDS = 15.0

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
    # webull.trade.common.order_type.OrderType lists TRAILING_STOP_LOSS as a
    # real order type (trailing_type/trailing_stop_step fields -- see
    # _order_payload). Wired in at the account owner's explicit instruction
    # ("I know for a fact trailing stop loss orders are allowed") ahead of
    # an independent live confirmation -- the SDK's own sample
    # (samples/trade/trade_client_v3.py) only demonstrates it against
    # market="HK", and scripts/verify_trailing_stop.py was built to close
    # that gap the same way verify_bracket_orders.py confirmed the plain
    # OCO bracket. Confirmed working in this account's real trading
    # (2026-08-12, per the account owner directly) -- this mapping is
    # correct for a US-market equity, not just a guess.
    OrderType.TRAILING_STOP: "TRAILING_STOP_LOSS",
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


def _round_to_valid_price_increment(price: float) -> float:
    """Rounds `price` to whatever tick size Webull actually accepts for an
    order, per the standard SEC Rule 612 sub-penny convention their own
    rejection message implies -- confirmed live 2026-08-12
    (`OAUTH_OPENAPI_STOCK_ORDER_PRICE_PRECISION_EXCEED`, HTTP 417, "Price
    increment should be 0.01 when price is equal to or greater than
    0.9999") on a resting OCO bracket's target/LIMIT leg for BIVI, whose
    `target_price` -- computed as `entry_price + risk_per_share *
    reward_risk_ratio` by every strategy in `strategy/*.py`, none of which
    round the result -- came out as an unrounded float
    (`3.4667600000000003`) with far more than 2 decimal digits. Every
    order this client sends carries a price computed somewhere upstream
    (strategy target math, RiskEngine/PositionManager stop math,
    OrderManager's extended-hours marketable-limit pricing) with no
    guarantee any of them already round cleanly -- rounding once here, at
    the single choke point every order's price passes through before
    being serialized to Webull, catches all of them rather than requiring
    every price-computing call site to remember to round itself. Below
    $1, sub-penny quoting is allowed (finer than $0.01) -- this mirrors
    that with 4 decimal places rather than assuming every stock needs the
    same tick size."""
    return round(price, 2) if price >= 1.0 else round(price, 4)


def _epoch_ms_to_dt(ms: Optional[int]) -> datetime:
    if not ms:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).replace(tzinfo=None)


_EASTERN = ZoneInfo("America/New_York")
_REGULAR_SESSION_OPEN = time(9, 30)
_REGULAR_SESSION_CLOSE = time(16, 0)


def _is_outside_regular_session(timestamp_utc: datetime) -> bool:
    """True when `timestamp_utc` (naive UTC, same convention as
    MarketSnapshot.timestamp) falls before 9:30am or at/after 4:00pm
    US/Eastern -- i.e. pre-market or after-hours.

    Used to gate _snapshot_from_dict's ext_price/ext_volume preference
    below on wall-clock time, not just on "the field happened to be present
    and non-zero": it's unconfirmed whether Webull actually zeroes those
    fields out the instant the regular session opens, or whether they keep
    reporting that morning's last pre-market value all day. If it's the
    latter and this gate didn't exist, every regular-session snapshot would
    silently use a stale pre-market price/volume instead of the live one --
    gating on time removes that risk outright regardless of which way
    Webull's own behavior turns out to be."""
    eastern_time = timestamp_utc.replace(tzinfo=timezone.utc).astimezone(_EASTERN).time()
    return eastern_time < _REGULAR_SESSION_OPEN or eastern_time >= _REGULAR_SESSION_CLOSE


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
        # Lazily created on the first subscribe_quotes() call -- see that
        # method's docstring. One persistent MQTT connection is reused
        # across every subsequent subscribe_quotes() call (adding more
        # symbols to the same connection) rather than opening a new one
        # per call.
        self._streaming_client: Optional[DataStreamingClient] = None
        # Tracked ourselves rather than trusted from
        # DataStreamingClient.get_connect_success() -- confirmed live
        # 2026-08-11 that method is misleadingly named: it flips True the
        # instant `client.on_connect_success = ...` is *assigned*
        # (DataStreamingClient's own property setter sets this
        # unconditionally, before any network call happens at all -- see
        # subscribe_quotes' docstring), not once the MQTT handshake
        # actually succeeds. Trusting it caused a real production bug:
        # multiple subscribe_quotes() calls landing in quick succession
        # (e.g. reconcile_positions_from_broker adopting several positions
        # in the same pass) would see the SDK's flag already "true" for
        # every call after the first and try to register a subscription
        # over REST before the MQTT session existed server-side -- the
        # exact 417 INVALID_SESSION failure mode from this feature's
        # original verification, just reintroduced by a different path.
        # This flag is set only inside this module's own _on_connect_success
        # wrapper below, which the SDK genuinely only calls once connected.
        self._streaming_connected: bool = False
        # Set right before each new connection attempt starts
        # (connect_and_loop_start()); consulted by subscribe_quotes to
        # decide when a still-not-connected client has been stuck long
        # enough to give up on and retry fresh -- see
        # _STREAMING_RECONNECT_DELAY_SECONDS.
        self._streaming_connect_attempted_at: Optional[datetime] = None
        self._streaming_subscribed_symbols: set[str] = set()
        # Per-symbol caches used to merge the two streamed message types
        # into one MarketSnapshot -- see subscribe_quotes' docstring for
        # why both are subscribed and why a merge is needed at all
        # (SNAPSHOT has no bid/ask; QUOTE has nothing else). Only
        # SNAPSHOT-derived fields live in _streaming_snapshot_cache
        # (bid/ask always 0.0 there, exactly _snapshot_from_streamed_result's
        # normal output); _streaming_quote_cache holds the latest top-of-book
        # (bid, ask, bid_size, ask_size) from the QUOTE feed. A merged
        # snapshot is only ever handed to on_update once both caches have
        # seen at least one real message for that symbol -- see
        # _merge_streamed_snapshot's docstring for why a bid=0/ask=0
        # snapshot must never reach a caller doing spread math.
        self._streaming_snapshot_cache: dict[str, MarketSnapshot] = {}
        self._streaming_quote_cache: dict[str, tuple[float, float, float, float]] = {}
        # Guards all four of the streaming dicts/sets above -- they're
        # written from whatever thread calls subscribe_quotes (TradingLoop's
        # main thread in practice) and read/written from the MQTT client's
        # own background network-loop thread inside _on_connect_success/
        # _on_quotes_message, which is exactly the kind of cross-thread
        # structural access this project already uses a dedicated lock for
        # elsewhere (see runtime/trading_loop.py's _candidates_lock and its
        # docstring).
        self._streaming_lock = threading.Lock()

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
        if self._streaming_client is not None:
            try:
                self._streaming_client.loop_stop()
                self._streaming_client.disconnect()
            except Exception:
                logger.warning("Failed to cleanly disconnect the streaming client.", exc_info=True)
            self._streaming_client = None
            with self._streaming_lock:
                self._streaming_connected = False
                self._streaming_connect_attempted_at = None
                self._streaming_subscribed_symbols = set()
                self._streaming_snapshot_cache = {}
                self._streaming_quote_cache = {}
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
        # NORMAL priority: feeds entry-sizing (RiskEngine.evaluate's
        # account_equity/account_buying_power), not exit management -- see
        # retry.py's CallPriority docstring for the full tier rationale.
        # Previously not rate-limited or retried AT ALL (unlike
        # market_data.* calls) despite sharing the exact same account-wide
        # ~1 req/s budget -- see this module's docstring and
        # retry.py's "Priority tiers" section for why every Webull call
        # this client makes now goes through the same shared, paced,
        # priority-aware limiter.
        response = call_with_retry(
            lambda: self._require_trade_client().account_v2.get_account_balance(self.account_id),
            priority=CallPriority.NORMAL,
        )
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
        # Confirmed live on 2026-08-10 against a real populated response
        # (see the module docstring's "account_v2.get_account_position"
        # entry for the exact row shape observed): `symbol`/`quantity`/
        # `cost_price` below are all confirmed correct. `symbol` still keeps
        # its multi-key fallback (rather than a bare `raw["symbol"]`, which
        # used to be a hard KeyError on any mismatch -- see get_positions'
        # docstring for the real production incident that caused) as cheap
        # insurance against a future response shape change, even though the
        # plain `"symbol"` key is now confirmed to be correct today.
        #
        # NOT confirmed, and known-wrong as written: there is no `side`
        # field in the real response at all, so `side_raw` below always
        # falls back to the "BUY" default -- a short position cannot
        # currently be reported as anything but a (wrong) long. No
        # confirmed alternate field to key off of yet; every real position
        # observed so far has been long, so this hasn't been forced to
        # matter in practice. Also no `open_time` field -- `opened_at`
        # below is never the real fill time.
        symbol = raw.get("symbol") or raw.get("ticker_symbol") or raw.get("instrument_symbol")
        if not symbol:
            raise KeyError("no recognized symbol field in position row")
        side_raw = raw.get("side", "BUY")
        side = OrderSide.SELL_SHORT if side_raw == "SHORT" else OrderSide.BUY
        return Position(
            symbol=symbol,
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
        """Confirmed live against the sandbox as an empty JSON list only --
        _position_from_dict's field-name guesses have never been checked
        against a real populated row (see its docstring). A production
        incident showed why that matters: a row this can't parse used to
        raise straight out of this method entirely, which meant EVERY
        caller lost EVERY position at once -- not just the one bad row --
        including RiskEngine.evaluate's own open_positions lookup (so a
        single unparseable row could silently block every future entry,
        too) and TradingLoop._confirm_entry_filled's post-fill reconciliation
        (a filled order at the broker never becoming a locally-tracked
        position, with no error surfaced anywhere obvious). Skipping and
        logging the one bad row instead keeps every other real position
        visible, and the logged raw row is exactly the data needed to fix
        _position_from_dict's field-name guesses the next time this actually
        fires against a real account.

        CRITICAL priority: called from both entry-sizing (RiskEngine's
        open_positions exposure check) AND exit-critical paths
        (post-fill confirmation, reconciliation, kill-switch flatten) --
        see retry.py's CallPriority docstring. Erring toward CRITICAL for
        every caller of this one shared method is deliberate: this call is
        cheap and infrequent relative to market-data polling, so always
        favoring it costs little contention-wise, while a discovery burst
        (BACKGROUND) never gets to starve out a position-visibility check
        that could be either kind of call under the hood. Previously not
        rate-limited or retried at all -- see _get_primary_currency_asset's
        comment for why that mattered."""
        response = call_with_retry(
            lambda: self._require_trade_client().account_v2.get_account_position(self.account_id),
            priority=CallPriority.CRITICAL,
        )
        response.raise_for_status()
        positions = []
        for row in response.json():
            try:
                positions.append(self._position_from_dict(row))
            except Exception:
                logger.exception("Failed to parse a position row from get_account_position; raw row: %r", row)
        return positions

    # -- market data ---------------------------------------------------------

    def _snapshot_from_dict(self, raw: dict, timestamp: Optional[datetime] = None) -> MarketSnapshot:
        last_price = float(raw["price"])
        cumulative_volume = float(raw.get("volume", 0) or 0)
        quote_timestamp = timestamp or _epoch_ms_to_dt(raw.get("quote_time"))

        # Prefer the extended-hours trade price/volume when the API actually
        # populated them AND the quote itself falls outside the regular
        # session (see _is_outside_regular_session) -- `price`/`volume`
        # alone are the regular-session fields and go stale/frozen-at-zero
        # the moment the regular session ends (price) or before it begins
        # (volume, which is legitimately 0 pre-9:30am even during active
        # pre-market trading), which would make every candidate's
        # displayed price AND every volume-derived metric (relative volume,
        # volume/dollar-volume acceleration, float velocity/turnover --
        # they're all built from cumulative_volume, see metrics/rolling.py)
        # silently wrong during pre-market or after-hours (exactly the
        # window PRE_MARKET/AFTER_MARKET discovery below is meant to catch).
        # The time gate exists because it's unconfirmed whether Webull
        # actually zeroes ext_price/ext_volume out once the regular session
        # opens, or whether they keep echoing that morning's last
        # pre-market value all day -- without the gate, the latter case
        # would silently corrupt regular-session data with stale pre-market
        # numbers. get_snapshot() is called with extend_hour_required=True
        # specifically so Webull includes these fields at all -- per that
        # method's docstring, they're omitted by default. Field names
        # (`ext_price`/`ext_volume`) are inferred from the SDK's protobuf
        # streaming schema's naming convention (webull/data/quotes/subscribe/
        # message_pb2.py: ext_price/ext_high/ext_low/ext_volume alongside the
        # regular price/high/low/volume), NOT confirmed against this REST
        # endpoint's actual JSON body -- the SDK has no schema/fixture for
        # that body to check against. If a key name is wrong, this silently
        # falls back to the regular-session field, same fail-soft handling
        # as every other optional field here; re-verify live during a real
        # pre-market or after-hours session before trusting this blindly.
        # (Webull also exposes a separate `overnight_required` flag /
        # `ovn_price`/`ovn_volume` fields for its newer overnight session --
        # deliberately not requested here, since only pre/after-market was
        # asked for.) One known, self-healing edge case: a rolling window
        # that straddles the 9:30am boundary will see cumulative_volume
        # apparently drop (ext_volume's pre-market total -> volume's
        # near-zero regular-session count), which metrics/rolling.py's
        # _volume_since clamps to 0 rather than negative -- a single
        # artificially-flat reading right at the open that clears itself
        # once the straddling snapshot ages out of the window.
        if _is_outside_regular_session(quote_timestamp):
            ext_price_raw = raw.get("ext_price")
            if ext_price_raw not in (None, "", "0", 0, 0.0):
                try:
                    last_price = float(ext_price_raw)
                except (TypeError, ValueError):
                    pass

            ext_volume_raw = raw.get("ext_volume")
            if ext_volume_raw not in (None, "", "0", 0, 0.0):
                try:
                    cumulative_volume = float(ext_volume_raw)
                except (TypeError, ValueError):
                    pass
        # Webull's snapshot endpoint does not return VWAP directly (confirmed
        # live -- the field is simply absent). Falling back to last_price
        # keeps distance_from_vwap_pct at a neutral 0 rather than fabricating
        # a biased number; replace once a real VWAP source is wired up
        # (e.g. accumulating price*volume from get_history_bar ourselves).
        vwap = last_price
        return MarketSnapshot(
            symbol=raw.get("symbol", ""),
            timestamp=quote_timestamp,
            last_price=last_price,
            bid=float(raw.get("bid", 0) or 0),
            ask=float(raw.get("ask", 0) or 0),
            bid_size=float(raw.get("bid_size", 0) or 0),
            ask_size=float(raw.get("ask_size", 0) or 0),
            cumulative_volume=cumulative_volume,
            vwap=vwap,
            high_of_day=float(raw.get("high", last_price)),
            low_of_day=float(raw.get("low", last_price)),
            open_price=float(raw.get("open", last_price)),
            premarket_high=None,  # not returned by this endpoint
            prev_close=float(raw["pre_close"]) if raw.get("pre_close") is not None else None,
        )

    def get_snapshot(self, symbol: str) -> MarketSnapshot:
        # extend_hour_required=True: per the SDK's own docstring, extended-
        # hours data (pre-market/after-hours) is NOT included by default --
        # without this, _snapshot_from_dict's ext_price handling above would
        # never see anything to prefer, and every candidate's price would
        # silently lag by hours during pre-market or after-hours trading.
        # NORMAL priority, fixed (not caller-configurable): this singular
        # method is on the strict BrokerClient ABC (see interfaces/broker.py),
        # so giving callers a way to override priority here would mean
        # updating every implementer (PaperBrokerClient, every test double)
        # to accept the kwarg -- not worth the blast radius when the
        # get_snapshots/get_raw_bars/get_daily_volumes methods below (all
        # optional/getattr-gated, so only WebullBrokerClient needs to know
        # about priority at all) already cover the two cases -- exit-
        # critical batch ticks vs. discovery -- where the distinction
        # actually matters in practice. See retry.py's CallPriority docstring.
        response = call_with_retry(
            lambda: self._require_data_client().market_data.get_snapshot(
                [symbol], Category.US_STOCK.name, extend_hour_required=True,
            ),
            priority=CallPriority.NORMAL,
        )
        response.raise_for_status()
        rows = response.json()
        if not rows:
            raise ValueError(f"Webull returned no snapshot for {symbol}")
        return self._snapshot_from_dict(rows[0])

    def get_snapshots(self, symbols: list[str], priority: int = CallPriority.NORMAL) -> dict[str, MarketSnapshot]:
        """Batch equivalent of get_snapshot -- fetches every symbol's
        current snapshot in as few Webull-paced round-trips as possible,
        chunked to _SNAPSHOT_BATCH_SIZE (the SDK's own documented per-
        request symbol cap: "up to 100 symbols" -- get_snapshot's docstring
        on the underlying market_data.get_snapshot), instead of the caller
        making one get_snapshot() call per symbol.

        This matters a lot in practice, not just in theory: every
        get_snapshot-family call -- 1 symbol or 100 -- shares the same
        globally-paced retry.webull_market_data_limiter (~1 req/s
        sustained, confirmed live, regardless of concurrency or request
        size). TradingLoop._process_all_candidates used to call
        get_snapshot once per tracked candidate every poll cycle, so N
        tracked candidates meant a real >=N-second floor on how often ANY
        single candidate's tick actually refreshes -- tens of seconds of
        staleness for a fast-moving low-float mover once the candidate list
        (now fed by 7 discovery sources) grows past a handful of names.
        Batching collapses that to ceil(N/100) rate-limited calls per
        cycle, each one counting as a single paced request regardless of
        how many symbols ride along in it.

        Not part of the BrokerClient interface (see interfaces/broker.py):
        this is a Webull-specific capability with no real backing need in
        paper/backtest mode (nothing there is rate-limited), so callers
        (TradingLoop) check for it with getattr rather than requiring every
        broker to implement it -- same pattern as get_raw_bars/
        get_daily_volumes.

        A symbol Webull doesn't return a row for (delisted, momentarily
        unavailable, a bad ticker, etc.) is simply absent from the result
        dict rather than raising -- callers must treat a missing key the
        same way they'd treat get_snapshot raising for that one symbol.

        `priority` (2026-08-11, see retry.py's CallPriority docstring):
        this one method serves two very differently-urgent call sites --
        TradingLoop._process_all_candidates' per-tick batch, which feeds
        MANAGING positions' stop/target/VWAP checks directly, and
        BroadScanner.scan()'s discovery-time batch, which only affects
        finding NEW candidates. Defaults to NORMAL; TradingLoop passes
        CRITICAL explicitly, BroadScanner passes BACKGROUND explicitly --
        so a discovery burst can never make an exit-critical batch wait
        behind it for the next rate-limiter slot."""
        if not symbols:
            return {}
        results: dict[str, MarketSnapshot] = {}
        for start in range(0, len(symbols), _SNAPSHOT_BATCH_SIZE):
            chunk = symbols[start:start + _SNAPSHOT_BATCH_SIZE]
            response = call_with_retry(
                lambda chunk=chunk: self._require_data_client().market_data.get_snapshot(
                    chunk, Category.US_STOCK.name, extend_hour_required=True,
                ),
                priority=priority,
            )
            response.raise_for_status()
            for row in response.json():
                snapshot = self._snapshot_from_dict(row)
                results[snapshot.symbol] = snapshot
        return results

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
        # NORMAL priority, fixed -- same rationale as get_snapshot's own
        # comment above (strict ABC method, not worth the blast radius of a
        # caller-configurable priority when nothing currently calls this
        # from an exit-critical path).
        timespan = _INTERVAL_TO_TIMESPAN.get(interval)
        if timespan is None:
            raise ValueError(f"Unsupported interval {interval!r}; supported: {sorted(_INTERVAL_TO_TIMESPAN)}")
        response = call_with_retry(
            lambda: self._require_data_client().market_data.get_history_bar(
                symbol, Category.US_STOCK.name, timespan, count=str(lookback)
            ),
            priority=CallPriority.NORMAL,
        )
        response.raise_for_status()
        return self._snapshots_from_bars(symbol, response.json())

    def get_raw_bars(self, symbol: str, interval: str, count: int, priority: int = CallPriority.BACKGROUND) -> list[dict]:
        """Each entry is that bar's OWN {time, open, high, low, close, volume}
        (not a running total), most-recent-first -- Webull's native response
        shape and order, completely unprocessed. This deliberately bypasses
        get_bars()/_snapshots_from_bars() above: that method accumulates
        volume across every fetched bar for intraday VWAP, which is correct
        for minute bars within one session but wrong for anything spanning
        multiple sessions/days (it would sum them together instead of
        reporting each bar's own total). Use this whenever a caller needs
        each bar's own numbers -- daily volume-per-day, a multi-day volume
        profile, etc.

        Confirmed live (2026-08-09): for a low-float/illiquid symbol,
        Webull returns the last `count` bars that actually have data, not
        the last `count` calendar time-slots -- e.g. requesting 100 5-minute
        bars for a liquid mega-cap (AAPL) spans about 1 day, while the same
        request for an illiquid mover (MB) reaches back ~25 calendar days to
        find 100 real (non-empty) bars. Callers that care about a bounded
        calendar lookback (not just "N most recent real bars") must filter
        the `time` field themselves -- this method does not.

        Not part of the BrokerClient interface (see interfaces/broker.py):
        this is a Webull-specific capability with no real backing data in
        paper/backtest mode, so callers (BroadScanner) check for it with
        getattr rather than requiring every broker to implement it.

        trading_sessions=["PRE", "RTH", "ATH"]: get_history_bar's own SDK
        docstring says "By default, only intraday [regular-session] candlestick
        data is returned" and documents a trading_sessions param to change
        that, but doesn't spell out the accepted values -- those are only
        written out on a sibling endpoint in the same SDK, get_footprint:
        "RTH: During trading hours, PRE: Before trading hours, ATH: After
        trading hours. Default: RTH." Reusing those three strings here is
        inferred by analogy across two endpoints of the same SDK, not
        confirmed against a real get_history_bar response that actually
        contains pre/after-market bars -- re-verify live during an actual
        pre-market or after-hours session (check returned bar timestamps
        fall outside 9:30am-4:00pm ET) before fully trusting this. Requesting
        all three sessions here (rather than leaving the RTH-only default)
        is what lets _compute_static_resistance_levels' volume profile
        (metrics/volume_profile.py) reflect pre/after-market activity, not
        just the regular session -- see BroadScanner._fetch_raw_bars. This
        is safe to share with _compute_opening_range_high (metrics/opening_range.py),
        which already time-filters to the 9:30am regular-session window on
        its own, so the extra pre/after-market bars just fall outside that
        window rather than corrupting it. get_bars()/_snapshots_from_bars()
        above (VWAP, momentum-score ticks) deliberately still uses the
        RTH-only default -- extending sessions there would change VWAP and
        MIS calculations beyond what was asked for.

        `priority` (2026-08-11, see retry.py's CallPriority docstring):
        every current caller of this method (resistance levels, volume
        profile, opening range, daily-volume history -- all via
        BroadScanner) is discovery/pre-entry work, never exit-critical, so
        BACKGROUND is a safe default; BroadScanner still passes it
        explicitly for clarity rather than relying on the default alone."""
        timespan = _INTERVAL_TO_TIMESPAN.get(interval)
        if timespan is None:
            raise ValueError(f"Unsupported interval {interval!r}; supported: {sorted(_INTERVAL_TO_TIMESPAN)}")
        response = call_with_retry(
            lambda: self._require_data_client().market_data.get_history_bar(
                symbol, Category.US_STOCK.name, timespan, count=str(count),
                trading_sessions=["PRE", "RTH", "ATH"],
            ),
            priority=priority,
        )
        response.raise_for_status()
        return response.json()

    def get_daily_volumes(self, symbol: str, lookback_days: int, priority: int = CallPriority.BACKGROUND) -> list[float]:
        """Each entry is that day's OWN volume (not a running total), most
        recent trading day first. Confirmed live (2026-08-09): raw daily
        bars come back most-recent-first with each bar's own `volume` field
        already distinct per day -- e.g. a real 5-day AAPL pull returned
        34.4M/46.1M/49.4M/68.0M/75.1M, one full day's volume per entry, not
        a cumulative sum. See get_raw_bars above for why this doesn't reuse
        get_bars()/_snapshots_from_bars()."""
        return [float(row["volume"]) for row in self.get_raw_bars(symbol, "1d", lookback_days, priority=priority)]

    def _snapshot_from_streamed_result(self, result) -> MarketSnapshot:
        """Maps a live-streamed SnapshotResult
        (webull.data.quotes.subscribe.snapshot_result.SnapshotResult) to
        this project's MarketSnapshot. Confirmed live 2026-08-11
        (scripts/verify_streaming.py --sub-type SNAPSHOT) against the real
        sandbox account -- exact attribute names read directly from the
        SDK's own snapshot_result.py source, not guessed:
        `.basic.symbol`/`.timestamp`/`.trading_session`, `.price`/`.open`/
        `.high`/`.low`/`.pre_close`/`.volume`, plus `ext_price`/`ext_high`/
        `ext_low`/`ext_volume` for extended hours -- the same `ext_*`
        naming convention already used by the REST get_snapshot mapping in
        `_snapshot_from_dict`, handled the same way here: prefer `ext_*`
        only when the timestamp falls outside the regular session (via the
        same `_is_outside_regular_session` gate, for the same reason --
        unconfirmed whether Webull zeroes these fields out at the open or
        echoes a stale pre-market value all day).

        No bid/ask -- this streaming message type doesn't carry top-of-
        book data at all (that's the separate `QUOTE` type -- see
        subscribe_quotes' docstring for how the two are merged into one
        MarketSnapshot before reaching a caller). Always 0.0 here; never
        pass this method's return value directly to `on_update` --
        `_merge_streamed_snapshot` is what actually assembles the value
        callers receive.

        `vwap` is not available from this feed either, matching the REST
        endpoint's own gap (see `_snapshot_from_dict`'s docstring) --
        falls back to `last_price`, the identical convention."""
        basic = result.basic
        quote_timestamp = _epoch_ms_to_dt(basic.timestamp)
        last_price = float(result.price) if result.price is not None else 0.0
        cumulative_volume = float(result.volume) if result.volume is not None else 0.0
        high = float(result.high) if result.high is not None else last_price
        low = float(result.low) if result.low is not None else last_price
        open_price = float(result.open) if result.open is not None else last_price

        if _is_outside_regular_session(quote_timestamp):
            if result.ext_price is not None:
                last_price = float(result.ext_price)
            if result.ext_volume is not None:
                cumulative_volume = float(result.ext_volume)
            if result.ext_high is not None:
                high = float(result.ext_high)
            if result.ext_low is not None:
                low = float(result.ext_low)

        return MarketSnapshot(
            symbol=basic.symbol,
            timestamp=quote_timestamp,
            last_price=last_price,
            bid=0.0,
            ask=0.0,
            bid_size=0.0,
            ask_size=0.0,
            cumulative_volume=cumulative_volume,
            vwap=last_price,
            high_of_day=high,
            low_of_day=low,
            open_price=open_price,
            prev_close=float(result.pre_close) if result.pre_close is not None else None,
        )

    def _quote_top_of_book(self, result) -> tuple[str, float, float, float, float]:
        """Maps a live-streamed QuoteResult
        (webull.data.quotes.subscribe.quote_result.QuoteResult) to
        (symbol, bid, ask, bid_size, ask_size) -- top-of-book only
        (`.asks[0]`/`.bids[0]`; `AskBidResult.price`/`.size`, read directly
        from the SDK's own ask_bid_result.py source). Confirmed live
        2026-08-11 (the very first verify_streaming.py run, before the
        SNAPSHOT sub_type existed): a real message decoded as
        `asks:[price:304.22,size:203],bids:[price:304.21,size:41]` --
        ask > bid as expected, one level each, best price first. Falls
        back to 0.0/0.0 if a side's level list is empty (thin/no quote at
        that instant) rather than raising -- same fail-soft convention as
        every other optional field mapped in this module."""
        asks = result.asks
        bids = result.bids
        ask = float(asks[0].price) if asks and asks[0].price is not None else 0.0
        ask_size = float(asks[0].size) if asks and asks[0].size is not None else 0.0
        bid = float(bids[0].price) if bids and bids[0].price is not None else 0.0
        bid_size = float(bids[0].size) if bids and bids[0].size is not None else 0.0
        return result.basic.symbol, bid, ask, bid_size, ask_size

    def _merge_streamed_snapshot(self, symbol: str) -> Optional[MarketSnapshot]:
        """Combines the latest cached SNAPSHOT-derived MarketSnapshot
        (price/OHLC/volume) with the latest cached QUOTE-derived top-of-
        book (bid/ask/bid_size/ask_size) for `symbol` into one complete
        MarketSnapshot -- or returns None if either side has never arrived
        yet for this symbol.

        Deliberately fails closed, not open: a snapshot with real
        price/volume but bid=0/ask=0 would make
        `metrics.calculations.bid_ask_spread` silently report a spread of
        0.0 (its documented behavior for a non-positive bid or ask -- see
        that function), which `CandidateWatcher.update`'s spread-eligibility
        gate would then read as "spread is fine" when the truth is simply
        "no real quote data yet." Returning None here instead makes that
        case indistinguishable from "nothing has streamed for this symbol
        at all" to `TradingLoop._get_streaming_snapshot`, which already
        falls back to a REST snapshot (with a real bid/ask) whenever it
        gets None -- so a symbol whose QUOTE stream hasn't caught up yet
        never gets treated as spread-safe on fabricated data."""
        with self._streaming_lock:
            base = self._streaming_snapshot_cache.get(symbol)
            quote = self._streaming_quote_cache.get(symbol)
        if base is None or quote is None:
            return None
        bid, ask, bid_size, ask_size = quote
        return _dataclass_replace(base, bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size)

    def _subscribe_symbols_in_batches(self, streaming_client, symbols: list[str]) -> None:
        """Chunks `symbols` to _STREAMING_SUBSCRIBE_BATCH_SIZE before
        calling streaming_client.subscribe() -- see that constant's
        comment for the real incident this fixes (an uncapped call
        rejected wholesale with TOO_MANY_SYMBOLS past 100 symbols).
        Best-effort per chunk: one chunk raising is logged and does NOT
        stop the remaining chunks from being attempted, so a single bad
        batch can't take down subscription for every other symbol in the
        same call -- callers that need to know about a failure at all
        (subscribe_quotes' own already_connected branch) still see it via
        this re-raising the LAST exception encountered, if any, after
        every chunk has been tried."""
        last_exception: Optional[Exception] = None
        for start in range(0, len(symbols), _STREAMING_SUBSCRIBE_BATCH_SIZE):
            chunk = symbols[start:start + _STREAMING_SUBSCRIBE_BATCH_SIZE]
            try:
                streaming_client.subscribe(chunk, Category.US_STOCK.name, ["QUOTE", "SNAPSHOT"])
            except Exception as exc:
                logger.warning("Streaming subscribe failed for a batch of %d symbol(s).", len(chunk), exc_info=True)
                last_exception = exc
        if last_exception is not None:
            raise last_exception

    def subscribe_quotes(self, symbols: list[str], on_update: Callable[[MarketSnapshot], None]) -> None:
        """Confirmed live and working (2026-08-11, see this module's
        docstring's "Streaming market data" note and
        scripts/verify_streaming.py) -- subscribes to both the `SNAPSHOT`
        (price/open/high/low/volume) and `QUOTE` (top-of-book bid/ask)
        streaming message types for `symbols`, merges the latest of each
        per symbol (see `_merge_streamed_snapshot`), and calls
        `on_update(snapshot)` with the merged result every time either
        message type updates for a symbol that already has data cached
        for the other type too. A symbol's very first message (of either
        type) is cached but does NOT trigger `on_update` yet -- only once
        both a real SNAPSHOT and a real QUOTE have been seen for it, so
        `on_update` never fires with a fabricated bid/ask=0.0.

        One persistent `DataStreamingClient`/MQTT connection is created
        lazily on the first call and reused for every subsequent call --
        a later call with new symbols adds them to the same connection's
        subscription (or, if the connection hasn't finished connecting
        yet, they're picked up by the connect callback once it does)
        rather than opening a second connection. Already-subscribed
        symbols in `symbols` are silently ignored (no duplicate
        subscribe call). "Has the connection finished connecting yet" is
        tracked via this module's own `self._streaming_connected` flag,
        NOT `DataStreamingClient.get_connect_success()` -- confirmed live
        2026-08-11 that the SDK's method is misleadingly named: its
        `on_connect_success` property setter sets that flag `True` the
        instant a callback is *assigned* (before `connect_and_loop_start()`
        is even called, let alone before the MQTT handshake completes),
        so it can never actually distinguish "still connecting" from
        "connected." Trusting it caused a real bug in production: several
        `subscribe_quotes` calls landing back-to-back for different
        symbols (e.g. `reconcile_positions_from_broker` adopting multiple
        positions in one pass) would see it already "true" for every call
        after the first and try to register a subscription over REST
        before the MQTT session existed server-side yet -- the same `417
        INVALID_SESSION` failure this feature's original verification
        already diagnosed once, reintroduced by a different path.

        `on_update` is called from the MQTT client's own background
        thread (paho-mqtt's network loop), NOT the caller's thread --
        callers that touch shared state from it must handle their own
        thread-safety (see `TradingLoop._on_streaming_snapshot`'s
        docstring for how this project does it). A mapping failure for
        one message, or `on_update` itself raising, is logged and
        swallowed so one bad message can't kill the whole stream.

        `mqtt_host`: sandbox mode explicitly uses the confirmed
        `_SANDBOX_MQTT_STREAMING_HOST` (`data-api.sandbox.webull.com` --
        the SDK's own bundled endpoint config has no sandbox entry for
        this at all, only production); live mode passes `None` to let the
        SDK auto-resolve, which is correct there since the one host that
        config *does* know about (`data-api.webull.com`) is the real
        production host.

        Known open issue (2026-08-11, not yet understood): an MQTT CONNACK
        return code 1 ("Protocol not supported") has appeared on the SDK's
        own automatic reconnect attempt a few seconds after every
        verify_streaming.py test run's shutdown sequence began, regardless
        of host/sub_type/outcome -- possibly a reconnect-during-shutdown
        artifact specific to that short-lived script rather than a sign of
        real instability, but not confirmed either way for a long-running
        connection. TradingLoop's own staleness check (falling back to
        REST polling for a symbol once its streamed data goes quiet -- see
        `_get_streaming_snapshot`) is the safety net if this does recur in
        production."""
        with self._streaming_lock:
            new_symbols = [s for s in symbols if s not in self._streaming_subscribed_symbols]
            if not new_symbols:
                return

        if self._streaming_client is not None:
            with self._streaming_lock:
                already_connected = self._streaming_connected
                connect_attempted_at = self._streaming_connect_attempted_at
            if already_connected:
                # Let a failure here propagate to the caller
                # (_ensure_streaming_subscribed) rather than swallowing it
                # -- see this method's docstring's "retry" note. Swallowing
                # it here used to mean TradingLoop believed these symbols
                # were subscribed (nothing told it otherwise) even though
                # the REST call never actually registered them, so they'd
                # silently never stream at all for the rest of the process.
                #
                # Real bug fixed 2026-08-12: this branch used to mark
                # new_symbols as subscribed (in the shared block above,
                # before this if/else even ran) BEFORE this call, not
                # after. A failure here still raised and propagated
                # correctly, but the symbols were already in
                # self._streaming_subscribed_symbols by then -- so the very
                # "retry" this exception is supposed to enable
                # (_ensure_streaming_subscribed calling back in here next
                # tick) recomputed new_symbols as EMPTY (already marked
                # subscribed) and returned immediately with no exception,
                # no actual subscribe attempt, and no error surfaced --
                # silently and permanently stuck on REST polling for that
                # symbol for the rest of the process, exactly the outcome
                # this whole propagate-don't-swallow design was meant to
                # prevent. Subscribing here BEFORE recording success (only
                # marking new_symbols subscribed once this call actually
                # returns) is what makes a second call with the same
                # symbols a genuine retry instead of a silent no-op.
                self._subscribe_symbols_in_batches(self._streaming_client, new_symbols)
                with self._streaming_lock:
                    self._streaming_subscribed_symbols.update(new_symbols)
                return
            # Still waiting on the connect callback (or about to discard a
            # stale one below and open a fresh client). Neither path makes
            # a synchronous subscribe call the way the already_connected
            # branch above does, so there's no success/failure to gate
            # this on -- record new_symbols as subscribed right away so
            # _on_connect_success's fresh read of
            # self._streaming_subscribed_symbols (once it fires) picks
            # them up too. A healthy connection (per every live test so
            # far) finishes in well under _STREAMING_RECONNECT_DELAY_SECONDS
            # -- if it's been pending longer than that, treat the attempt
            # as dead rather than waiting on a callback that may never
            # fire, and retry with a fresh client below instead of
            # returning early. Every symbol ever requested
            # (self._streaming_subscribed_symbols, already updated with
            # new_symbols above) gets resubscribed against the new
            # connection once it connects, not just new_symbols.
            with self._streaming_lock:
                self._streaming_subscribed_symbols.update(new_symbols)
            stale = (
                connect_attempted_at is not None
                and datetime.utcnow() - connect_attempted_at > timedelta(seconds=_STREAMING_RECONNECT_DELAY_SECONDS)
            )
            if not stale:
                # _on_connect_success will subscribe the full updated set
                # (read fresh from self._streaming_subscribed_symbols) once
                # it fires.
                return
            logger.warning(
                "Streaming connection has been stuck connecting for over %.0fs; discarding it and "
                "retrying with a fresh connection.", _STREAMING_RECONNECT_DELAY_SECONDS,
            )
            try:
                self._streaming_client.loop_stop()
                self._streaming_client.disconnect()
            except Exception:
                logger.warning("Failed to cleanly tear down the stale streaming client.", exc_info=True)
            self._streaming_client = None
            with self._streaming_lock:
                self._streaming_connected = False
                self._streaming_connect_attempted_at = None
            # Falls through to create a fresh client below.
        else:
            # No streaming client at all yet -- the very first call ever.
            # Same reasoning as the "still waiting on the connect callback"
            # branch above: nothing synchronous happens on this path either,
            # so record new_symbols as subscribed now rather than gating on
            # a call that doesn't exist here, so _on_connect_success (below)
            # picks them up once the fresh client this falls through to
            # create actually connects.
            with self._streaming_lock:
                self._streaming_subscribed_symbols.update(new_symbols)

        session_id = str(uuid.uuid4())
        mqtt_host = (
            _SANDBOX_MQTT_STREAMING_HOST if self.settings.trading_mode == TradingMode.SANDBOX else None
        )

        def _on_connect_success(client_, api_client_, session_id_):
            with self._streaming_lock:
                self._streaming_connected = True
                current_symbols = sorted(self._streaming_subscribed_symbols)
            logger.info(
                "Streaming MQTT connected (session_id=%s); subscribing to %d symbol(s).",
                session_id_, len(current_symbols),
            )
            try:
                self._subscribe_symbols_in_batches(client_, current_symbols)
            except Exception:
                logger.exception("Streaming subscribe failed on connect for %s", current_symbols)

        def _on_quotes_message(client_, topic, payload):
            if topic == "snapshot":
                try:
                    snapshot = self._snapshot_from_streamed_result(payload)
                except Exception:
                    logger.exception("Failed to map a streamed snapshot payload: %r", payload)
                    return
                with self._streaming_lock:
                    self._streaming_snapshot_cache[snapshot.symbol] = snapshot
                symbol = snapshot.symbol
            elif topic == "quote":
                try:
                    symbol, bid, ask, bid_size, ask_size = self._quote_top_of_book(payload)
                except Exception:
                    logger.exception("Failed to map a streamed quote payload: %r", payload)
                    return
                with self._streaming_lock:
                    self._streaming_quote_cache[symbol] = (bid, ask, bid_size, ask_size)
            else:
                return

            merged = self._merge_streamed_snapshot(symbol)
            if merged is None:
                return  # haven't seen both message types for this symbol yet
            try:
                on_update(merged)
            except Exception:
                logger.exception("subscribe_quotes on_update callback raised for %s", symbol)

        client = DataStreamingClient(
            self.settings.webull.app_key, self.settings.webull.app_secret, _REGION, session_id,
            http_host=self.settings.webull.base_url,
            mqtt_host=mqtt_host,
        )
        client.on_connect_success = _on_connect_success
        client.on_quotes_message = _on_quotes_message
        with self._streaming_lock:
            self._streaming_connect_attempted_at = datetime.utcnow()
        client.connect_and_loop_start()
        self._streaming_client = client

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
            # Read from Settings (2026-08-12; was hardcoded "CORE" before)
            # so a candidate value can be tested live via
            # WEBULL_SUPPORT_TRADING_SESSION + a restart, no deploy needed
            # -- see Settings.webull_support_trading_session's docstring.
            # Defaults to "CORE" in code (unchanged, deliberately
            # conservative -- see below), but "ALL" is now CONFIRMED LIVE TO
            # WORK, reversing the 2026-08-10 finding below. History:
            # confirmed live on 2026-08-10 that this account/endpoint
            # REJECTED "ALL" outright: OAUTH_OPENAPI_PARAM_ERR ("Parameter
            # error, invalid support_trading_session, value: ALL"), HTTP
            # 417 -- contradicting Webull's own public OpenAPI docs, which
            # document "ALL" as the value for pre-market+core+after-hours.
            # Re-tested live on 2026-08-12 at ~4:21am ET (genuine pre-market,
            # sandbox mode, scripts/verify_extended_hours_order.py), with a
            # clean (non-rate-limited) request this time: "ALL" was
            # ACCEPTED and cleanly cancelled. Same run also confirmed
            # "NIGHT" is a real, correctly-scoped value -- rejected at that
            # same moment with a clear, specific error ("Overnight Trading
            # is only available during the Overnight Session, which
            # operates from 8:00pm to 4:00am ET"), not a param error, and
            # ACCEPTED live the night before (2026-08-11 ~8:54pm ET,
            # genuinely inside that window, modulo an unrelated
            # "FIXGW_NOT_READY" infra hiccup that run). Best-guess
            # explanation for the reversal: an account-level extended-hours
            # entitlement was enabled between 2026-08-10 and 2026-08-12 (a
            # common brokerage gate), not a code or documentation error on
            # either side. Also notable: "CORE" was ALSO accepted during
            # this same pre-market test -- undermining (not disproving --
            # order ACCEPTANCE and order MATCHING/fill are different
            # questions, and this test only exercised the former) the
            # leading diagnosis behind the end-of-core-hours auto-flatten
            # timing fix (is_within_closing_buffer, see market_hours.py)
            # that a "CORE"-scoped order needs a still-live CORE session to
            # even be accepted. That fix stays as a safety margin regardless
            # -- this doesn't reopen it, just softens the certainty of why
            # it was needed. IMPORTANT CAVEAT: all of the above was tested
            # in TRADING_MODE=sandbox, not live -- do not assume live-account
            # entitlements match sandbox without an equivalent live test.
            # Given "ALL" is now confirmed (sandbox), it's safe to set
            # WEBULL_SUPPORT_TRADING_SESSION=ALL via env var/.env for a
            # deployment that wants pre-market/after-hours entries, and only
            # then turn on RiskConfig.allow_extended_hours_trading from the
            # dashboard -- the code default here stays "CORE" rather than
            # flipping automatically, so existing deployments don't change
            # behavior without an explicit opt-in.
            #
            # NOT a static passthrough of that setting, though -- confirmed
            # live 2026-08-12 (post sandbox-reset, ~9:49am ET, genuinely
            # inside core hours) that "ALL" is REJECTED outright during
            # core hours specifically (OAUTH_OPENAPI_PARAM_ERR, HTTP 417,
            # "Parameter error, invalid support_trading_session, value:
            # ALL"), while working fine outside core hours (the 2026-08-12
            # pre-market confirmation above). So "ALL" isn't a blanket
            # account entitlement after all -- it's only valid outside core
            # hours, and a WEBULL_SUPPORT_TRADING_SESSION=ALL deployment
            # that submits an order during core hours needs "CORE" for that
            # one order instead, or every core-hours entry/exit fails with
            # this param error. Force "CORE" whenever `order.created_at`
            # (order submission time) falls inside core hours, regardless
            # of the configured value; only use the configured value
            # (typically "ALL") outside core hours, where it's actually
            # accepted.
            "support_trading_session": (
                "CORE" if is_within_core_trading_hours(order.created_at)
                else self.settings.webull_support_trading_session
            ),
            "entrust_type": "QTY",
            "client_order_id": client_order_id,
        }
        if order.limit_price is not None:
            payload["limit_price"] = str(_round_to_valid_price_increment(order.limit_price))
        if order.stop_price is not None:
            # Confirmed live 2026-08-11 (scripts/verify_bracket_orders.py,
            # via place_oco_bracket's resting STOP_LOSS leg, which builds
            # its payload through this exact same branch) and reconfirmed
            # by the account owner directly (2026-08-12) -- this field
            # name is correct, not a guess. (Earlier revisions of this
            # comment flagged it UNVERIFIED before that confirmation
            # existed; left this note rather than silently deleting the
            # history in case it's useful context for why the caution
            # existed at all.)
            payload["stop_price"] = str(_round_to_valid_price_increment(order.stop_price))
        if order.trailing_pct is not None:
            # PERCENTAGE-only -- see Order.trailing_pct's docstring for why
            # this model has no AMOUNT-mode field to map from. Field names
            # per webull.trade.request.place_order_request.py's
            # set_trailing_type/set_trailing_stop_step and confirmed
            # present in the SDK's own TRAILING_STOP_LOSS sample -- whether
            # this account/endpoint actually ACCEPTS them for a US-market
            # equity is the open question scripts/verify_trailing_stop.py
            # exists to answer (see _ORDER_TYPE_TO_WEBULL's docstring).
            payload["trailing_type"] = "PERCENTAGE"
            payload["trailing_stop_step"] = str(order.trailing_pct)
        return payload

    def place_order(self, order: Order) -> Order:
        if self.is_live and not self.settings.is_live_trading_authorized():
            # Defense in depth: even though __init__ already checked this, a
            # long-lived process could have its settings reloaded/mutated.
            raise RuntimeError("Live trading authorization lost; refusing to place order.")

        # CRITICAL priority, fixed: every place_order call is a real
        # trading action (an entry or a software-submitted exit), never
        # discovery/background work -- see retry.py's CallPriority
        # docstring. Previously not rate-limited or retried AT ALL despite
        # sharing the same account-wide budget as market-data calls -- a
        # burst of get_snapshot polling could 429 an entry/exit order with
        # nothing to catch it (see this module's docstring's "Priority
        # tiers" note and retry.py's for the fuller history).
        #
        # webull_limiter.exclusive(): real incident (CYCU/SCKT/BIVI,
        # 2026-08-12) showed CRITICAL priority alone isn't enough once
        # SEVERAL genuinely CRITICAL calls are in flight simultaneously
        # (a stuck exit retry, a bracket-attach retry, reconcile's
        # get_positions, ...) -- they still compete with each other for
        # the same ~1 req/s account-wide ceiling. Placing an order is the
        # single highest-stakes moment this client has; giving it
        # exclusive access to the rate-limit budget for the duration of
        # this call (including its own internal call_with_retry attempts)
        # means it's never competing with concurrent discovery/reconcile/
        # other-order traffic for the same slots. See RateLimiter.
        # exclusive's docstring.
        payload = self._order_payload(order)
        with webull_limiter.exclusive():
            response = call_with_retry(
                lambda: self._require_trade_client().order_v3.place_order(self.account_id, [payload]),
                priority=CallPriority.CRITICAL,
            )
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

    def place_oco_bracket(self, stop_order: Order, target_order: Order) -> tuple[Order, Order]:
        """Places a 2-leg OCO combo -- a resting protective STOP_LOSS leg
        and a resting LIMIT take-profit leg sharing one
        client_combo_order_id, so a fill on either leg cancels its sibling
        at the broker automatically. Confirmed live 2026-08-11 (see
        scripts/verify_bracket_orders.py) that Webull accepts attaching
        this combo_type=OCO pair to an ALREADY-OPEN position with no
        MASTER (entry) leg needed -- exactly the shape still needed for
        every call site of this method today: re-protecting a position
        after a partial exit and re-syncing the resting stop as breakeven/
        trailing math moves it (see TradingLoop._attach_broker_bracket),
        both of which necessarily happen well after the entry already
        filled, so there's no entry order left to bundle a MASTER leg
        with. NOT used for the very first bracket at fresh-entry time
        anymore, though -- see place_bracket_entry below, added 2026-08-13,
        for that MASTER-anchored atomic shape, which this method's
        docstring used to describe as deliberately avoided (a decision
        since reversed at the account owner's explicit instruction; see
        that method's docstring for why).

        order_v3.place_order accepts a *list* of order dicts sharing one
        client_combo_order_id as a combo -- a lone dict with
        combo_type=NORMAL (what place_order above sends) is a different,
        non-combo request shape, so this bypasses place_order's single-
        order path entirely while still reusing _order_payload per leg so
        side/type/time-in-force/support_trading_session stay identical to
        every other order this client places.

        Not part of the BrokerClient interface (see interfaces/broker.py)
        -- resting broker-side orders only mean something against a real
        broker; PaperBrokerClient/backtests fill every order synchronously
        at market with nothing to rest against. Callers (OrderManager, on
        TradingLoop's behalf -- see order_manager.py's
        _broker_supports_resting_orders) check for this with getattr, same
        pattern as get_snapshots/get_raw_bars."""
        if self.is_live and not self.settings.is_live_trading_authorized():
            raise RuntimeError("Live trading authorization lost; refusing to place order.")

        combo_id = str(uuid.uuid4())
        stop_payload = self._order_payload(stop_order)
        stop_payload["combo_type"] = "OCO"
        stop_payload["client_combo_order_id"] = combo_id
        target_payload = self._order_payload(target_order)
        target_payload["combo_type"] = "OCO"
        target_payload["client_combo_order_id"] = combo_id

        # CRITICAL priority: attaching/replacing a protective stop+target
        # bracket is a real trading action protecting an open position --
        # same tier as place_order above. Same webull_limiter.exclusive()
        # rationale as place_order too -- see that method's comment.
        with webull_limiter.exclusive():
            response = call_with_retry(
                lambda: self._require_trade_client().order_v3.place_order(
                    self.account_id, [stop_payload, target_payload]
                ),
                priority=CallPriority.CRITICAL,
            )
        response.raise_for_status()

        now = datetime.utcnow()
        for order, payload in ((stop_order, stop_payload), (target_order, target_payload)):
            # Same convention as place_order above: Webull's cancel/detail
            # calls key off the client-generated client_order_id, so
            # broker_order_id is set to that same value rather than
            # parsing anything out of the response body (whose success
            # shape for a combo is, like place_order's, unconfirmed).
            order.client_order_id = payload["client_order_id"]
            order.broker_order_id = payload["client_order_id"]
            order.status = OrderStatus.SUBMITTED
            order.updated_at = now
        return stop_order, target_order

    def place_bracket_entry(
        self, entry_order: Order, stop_order: Order, target_order: Optional[Order],
    ) -> tuple[Order, Order, Optional[Order]]:
        """Places the entry order together with its protective stop (and,
        quantity permitting, its take-profit target) as ONE atomic
        MASTER+STOP_LOSS(+STOP_PROFIT) combo -- the "Buy-to-Open TP/SL"
        shape from Webull's own OpenAPI docs. Added 2026-08-13 (see
        docs/ARCHITECTURE.md's "Atomic bracket entry" section) to close the
        unprotected window that existed between a fresh entry's fill and
        place_oco_bracket's separate, after-the-fact call above: that
        method's own docstring explains the PRIOR reasoning for keeping
        entry and bracket as two separate calls (touching the entry-fill
        pipeline was judged riskier than a short software-managed window)
        -- this reverses that call, at the account owner's explicit
        instruction, because the "short window" wasn't actually bounded
        (see TradingLoop._attach_broker_bracket's docstring: a failed
        attach retries forever with no ceiling) and outside core hours it
        wasn't short at all (the broker-side bracket is never even
        attempted then). Submitting all legs in the SAME request means
        there is no interval, ever, where a filled position during core
        hours exists without a resting broker-side bracket already placed
        against it.

        target_order may be None -- mirrors
        TradingLoop._attach_broker_bracket's own halving rule (the
        take-profit leg is a partial exit, sells half the position, so an
        entry too small to split into a whole-share partial exit gets a
        MASTER+STOP_LOSS combo with no take-profit leg at all, rather than
        a target leg for zero shares). Webull's combo_type table allows
        0-1 STOP_PROFIT legs per combo.

        No try/except here, deliberately: a rejection must propagate to
        the caller as a hard failure. Per explicit instruction (2026-08-13):
        if this call fails, the trade must NOT go through at all -- no
        fallback to a plain, unprotected entry. See
        OrderManager.submit_entry_signal and
        TradingLoop._submit_entry's BracketEntryRejected handling, which is
        exactly what this raising (instead of swallowing, unlike
        _attach_broker_bracket's own best-effort contract) exists to feed.

        Same "response body's success shape is UNVERIFIED beyond
        raise_for_status" caveat as place_order/place_oco_bracket applies
        here too -- a 2xx response means Webull accepted the combo request
        for processing, not that every individual leg is independently
        confirmed. A leg that silently failed despite an overall 2xx would
        still be caught by this loop's existing defense-in-depth
        (_sync_broker_protective_orders' every-tick "no resting order yet"
        retry, and normal fill-confirmation polling for the entry leg
        itself)."""
        if self.is_live and not self.settings.is_live_trading_authorized():
            raise RuntimeError("Live trading authorization lost; refusing to place order.")

        combo_id = str(uuid.uuid4())
        entry_payload = self._order_payload(entry_order)
        entry_payload["combo_type"] = "MASTER"
        entry_payload["client_combo_order_id"] = combo_id

        stop_payload = self._order_payload(stop_order)
        stop_payload["combo_type"] = "STOP_LOSS"
        stop_payload["client_combo_order_id"] = combo_id

        payloads = [entry_payload, stop_payload]
        target_payload = None
        if target_order is not None:
            target_payload = self._order_payload(target_order)
            target_payload["combo_type"] = "STOP_PROFIT"
            target_payload["client_combo_order_id"] = combo_id
            payloads.append(target_payload)

        # CRITICAL priority + exclusive(): same rationale as place_order/
        # place_oco_bracket above -- this is the single highest-stakes call
        # this client makes (it's now BOTH the entry and its protection at
        # once), so it must never compete with concurrent discovery/
        # reconcile/other-order traffic for the same rate-limit slots.
        with webull_limiter.exclusive():
            response = call_with_retry(
                lambda: self._require_trade_client().order_v3.place_order(self.account_id, payloads),
                priority=CallPriority.CRITICAL,
            )
        response.raise_for_status()

        now = datetime.utcnow()
        for order, payload in ((entry_order, entry_payload), (stop_order, stop_payload)):
            # Same convention as place_order/place_oco_bracket: Webull's
            # cancel/detail calls key off the client-generated
            # client_order_id, so broker_order_id is set to that same
            # value rather than parsing anything out of the response body.
            order.client_order_id = payload["client_order_id"]
            order.broker_order_id = payload["client_order_id"]
            # SUBMITTED, not FILLED -- same as place_order: a 2xx means
            # accepted for processing. The entry leg still needs normal
            # fill-confirmation polling exactly as before
            # (_poll_pending_entry/_maybe_verify_entry_via_positions);
            # this call only changes WHEN the bracket gets placed, not how
            # the entry's own fill gets confirmed.
            order.status = OrderStatus.SUBMITTED
            order.updated_at = now
        if target_order is not None:
            target_order.client_order_id = target_payload["client_order_id"]
            target_order.broker_order_id = target_payload["client_order_id"]
            target_order.status = OrderStatus.SUBMITTED
            target_order.updated_at = now

        return entry_order, stop_order, target_order

    def cancel_order(self, broker_order_id: str) -> None:
        # CRITICAL priority: always either protecting a position (cancelling
        # a stale resting stop/target before replacing or before a full
        # exit) or genuine cleanup -- never background work.
        response = call_with_retry(
            lambda: self._require_trade_client().order_v3.cancel_order(self.account_id, broker_order_id),
            priority=CallPriority.CRITICAL,
        )
        response.raise_for_status()

    def modify_order(self, broker_order_id: str, **changes) -> Order:
        # UNVERIFIED request/response shape -- no live order existed to test
        # a replace against during integration. modify_orders is confirmed
        # to be a list-of-dicts like place_order's new_orders (same
        # ReplaceOrderRequest.set_modify_orders pattern), but the exact
        # accepted keys beyond client_order_id were not confirmed live.
        # NORMAL priority: not used by any production path today (cancel +
        # place_resting_stop/place_resting_bracket is used instead -- see
        # TradingLoop._sync_broker_protective_orders' docstring for why),
        # only by scripts/verify_bracket_orders.py.
        modify_payload = {"client_order_id": broker_order_id, **changes}
        response = call_with_retry(
            lambda: self._require_trade_client().order_v3.replace_order(self.account_id, [modify_payload]),
            priority=CallPriority.NORMAL,
        )
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
            # Added alongside stop_price becoming a real, exercised field
            # (scripts/verify_bracket_orders.py) -- same UNVERIFIED status
            # as limit_price above, just never had a reason to be read back
            # before now since no order carrying a stop_price had ever been
            # placed live.
            stop_price=float(raw["stop_price"]) if raw.get("stop_price") is not None else None,
            status=status,
            client_order_id=raw.get("client_order_id"),
            broker_order_id=raw.get("client_order_id"),
        )

    def get_order_status(self, broker_order_id: str) -> Order:
        # CRITICAL priority, fixed: the majority and most important callers
        # of this method are exit/fill-critical (pending-exit polling,
        # broker-bracket fill polling) -- the minority use (pending-entry
        # polling, not yet money-at-risk) isn't harmed by also being
        # favored, unlike the reverse (an entry-priority default starving
        # an exit poll would be a real problem). Strict ABC method, so not
        # caller-configurable -- see get_snapshot's comment for why that
        # tradeoff was made instead of threading priority through here too.
        response = call_with_retry(
            lambda: self._require_trade_client().order_v3.get_order_detail(self.account_id, broker_order_id),
            priority=CallPriority.CRITICAL,
        )
        response.raise_for_status()
        body = response.json()
        row = body[0] if isinstance(body, list) else body
        return self._order_from_detail(row)

    def poll_fills(self, since: Optional[datetime] = None) -> list[Fill]:
        # UNVERIFIED field names for a populated response -- no live
        # execution existed to test against during integration. NORMAL
        # priority: only used to look up a trade's realized exit price
        # after the fact (_build_trade_from_fill), not blocking any live
        # decision.
        start_date = since.strftime("%Y-%m-%d") if since else None
        response = call_with_retry(
            lambda: self._require_trade_client().order_v3.get_order_executions(
                self.account_id, start_date=start_date
            ),
            priority=CallPriority.NORMAL,
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

    def _order_from_open_order_dict(self, raw: dict) -> Order:
        """get_order_open's confirmed-live row shape (2026-08-11, this
        session, via a real resting OCO bracket) uses `total_quantity`, NOT
        the plain `quantity` key get_order_detail/place_order's own
        payloads use elsewhere in this file -- reusing _order_from_detail
        here unmodified would have silently mapped every open order to
        quantity=0. Falls back to `quantity` defensively (same multi-key-
        fallback insurance as _position_from_dict's `symbol` lookup) in
        case a different order type/response variant ever uses that key
        instead."""
        status = _WEBULL_STATUS_TO_OURS.get(raw.get("status", ""), OrderStatus.PENDING)
        return Order(
            symbol=raw.get("symbol", ""),
            side=OrderSide.BUY if raw.get("side") == "BUY" else OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=float(raw.get("total_quantity", raw.get("quantity", 0))),
            limit_price=float(raw["limit_price"]) if raw.get("limit_price") is not None else None,
            stop_price=float(raw["stop_price"]) if raw.get("stop_price") is not None else None,
            status=status,
            client_order_id=raw.get("client_order_id"),
            broker_order_id=raw.get("client_order_id"),
        )

    def list_open_orders(self, priority: int = CallPriority.NORMAL) -> list[Order]:
        """Fetches every currently-resting (SUBMITTED, not yet filled/
        cancelled) order for this account in one call, via
        order_v3.get_order_open(account_id, page_size=None,
        last_client_order_id=None) -- confirmed live 2026-08-11 as the
        REAL "list open orders" method (a ChatGPT-sourced guess suggested
        `get_open_orders`/`GET /openapi/trade/order/open`; the real method
        name and REST path, `GET /trading/orders/open-orders/list`, were
        confirmed independently via both the official OpenAPI spec and
        introspecting the actual installed SDK -- treat that as the
        standing example of why a third-party AI's guess about this SDK is
        never trusted here without independent confirmation).

        Used by TradingLoop to check every broker-managed position's
        resting stop/target legs in ONE call per tick instead of one
        get_order_status call per leg per position -- see
        TradingLoop._get_open_orders_for_tick/_poll_broker_bracket's
        docstrings for how the result is used (a leg still present here is
        confirmed still resting, with no further call needed; a leg that's
        disappeared needs exactly one follow-up get_order_status call to
        learn whether it filled or was cancelled, since this endpoint only
        says "no longer open," not why).

        Single page only -- pagination via last_client_order_id is NOT
        implemented here; this bot's expected number of concurrently-open
        positions is small enough that a single page should cover every
        resting order in practice, but this is an assumption, not a
        confirmed server-side page size limit. Revisit if a real account
        ever has enough simultaneous resting orders to risk truncation.

        Not part of the BrokerClient interface (see interfaces/broker.py)
        -- same getattr-gated pattern as get_snapshots/get_raw_bars/
        place_oco_bracket: PaperBrokerClient/backtests have no resting
        orders to list at all."""
        response = call_with_retry(
            lambda: self._require_trade_client().order_v3.get_order_open(self.account_id),
            priority=priority,
        )
        response.raise_for_status()
        body = response.json()
        rows = body if isinstance(body, list) else [body]
        return [self._order_from_open_order_dict(row) for row in rows]

    @property
    def is_live(self) -> bool:
        return self.settings.trading_mode == TradingMode.LIVE
