"""
MT5Client -- the only module that speaks MetaTrader5's native shapes.
Everything else in this project (relay_client.py, main.py) only ever
sees relay_protocol's Wire* models in and out. Takes the real-or-fake
`mt5` module via constructor injection (never a bare top-level `import
MetaTrader5`), so this stays importable and testable on Linux -- see
main.py's `_import_real_mt5()` for the one place that import actually
happens.

Grounded against MetaQuotes' official MQL5 docs (see docs/ARCHITECTURE.md's
Phase 5d section in the main forex-scalper-bot project for the full
citation trail). Several mappings here are explicitly flagged as
compromises or unverified-without-real-hardware rather than presented as
settled -- see each section's own comment.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from relay_protocol.wire_models import WireFill, WireMarketSnapshot, WireOrder, WirePosition

from .symbols import mt5_symbol_to_wire_pair, wire_pair_to_mt5_symbol


class MT5ClientError(Exception):
    """Base for every MT5Client-specific failure."""


class MT5ConnectionError(MT5ClientError):
    """initialize()/login() failed, or account_info() unexpectedly
    returned None on a call that requires an active terminal session."""


class MT5SymbolError(MT5ClientError):
    """symbol_select/symbol_info_tick couldn't produce data for a symbol."""


class MT5UnknownOrderError(MT5ClientError):
    """broker_order_id doesn't match anything in the in-memory order
    registry -- either it was never placed by this process, or this
    process has restarted since (see this module's docstring on the
    registry's in-memory-only limitation)."""


class MT5OrderRejectedError(MT5ClientError):
    """A genuine MT5 trading rejection (order_send's retcode was not
    TRADE_RETCODE_DONE). Raised, never quietly turned into a
    'rejected'-status WireOrder returned as a normal response -- the
    cloud's BrokerRejectedError only ever fires off an `error`-kind wire
    envelope, which relay_client.py's dispatch loop only produces when a
    handler raises."""

    def __init__(self, retcode: int, comment: str, error_type: str = "OrderRejected"):
        super().__init__(f"MT5 retcode {retcode}: {comment}")
        self.retcode = retcode
        self.comment = comment
        self.error_type = error_type


@dataclass
class _OrderRecord:
    client_order_id: Optional[str]
    strategy_name: Optional[str]
    signal_id: Optional[str]
    wire_order: WireOrder


# retcode -> a useful-but-not-overclaimed error_type classification.
# Deliberately doesn't attempt retryable-vs-terminal semantics yet --
# real MT5 retcode vocabulary needs Phase 5g confirmation first, same
# discipline local_connector/exceptions.py already applies cloud-side.
_RETCODE_ERROR_TYPES = {
    10004: "Requote",
    10006: "OrderRejected",
    10016: "InvalidStops",
    10017: "TradeDisabled",
    10018: "MarketClosed",
    10019: "InsufficientMargin",
    10020: "PriceChanged",
    10021: "PriceOff",
}

_MARKET_ORDER_TYPE_ATTR = {"buy": "ORDER_TYPE_BUY", "sell": "ORDER_TYPE_SELL"}
_LIMIT_ORDER_TYPE_ATTR = {"buy": "ORDER_TYPE_BUY_LIMIT", "sell": "ORDER_TYPE_SELL_LIMIT"}
_STOP_ORDER_TYPE_ATTR = {"buy": "ORDER_TYPE_BUY_STOP", "sell": "ORDER_TYPE_SELL_STOP"}
_STOP_LIMIT_ORDER_TYPE_ATTR = {"buy": "ORDER_TYPE_BUY_STOP_LIMIT", "sell": "ORDER_TYPE_SELL_STOP_LIMIT"}

_FILLING_ATTR_FOR_TIF = {"ioc": "ORDER_FILLING_IOC", "fok": "ORDER_FILLING_FOK"}

_INTERVAL_TO_TIMEFRAME_ATTR = {
    "1m": "TIMEFRAME_M1", "5m": "TIMEFRAME_M5", "15m": "TIMEFRAME_M15", "30m": "TIMEFRAME_M30",
    "1h": "TIMEFRAME_H1", "4h": "TIMEFRAME_H4", "1d": "TIMEFRAME_D1", "1w": "TIMEFRAME_W1",
}


def _order_comment(client_order_id: Optional[str]) -> str:
    # MT5's comment field is historically capped around 31 characters --
    # far too short for a full UUID. Purely a human-debugging aid in the
    # terminal; the REAL client_order_id<->ticket mapping is
    # MT5Client._order_registry, keyed by the actual MT5 order ticket.
    if not client_order_id:
        return ""
    return client_order_id.replace("-", "")[:16]


def _parse_ticket(broker_order_id: str) -> int:
    try:
        return int(broker_order_id)
    except (TypeError, ValueError):
        raise MT5UnknownOrderError(f"broker_order_id {broker_order_id!r} is not a valid MT5 ticket.") from None


class MT5Client:
    def __init__(
        self, mt5_module: Any, *,
        login: Optional[int] = None, password: Optional[str] = None, server: Optional[str] = None,
        symbol_suffix: str = "", path: Optional[str] = None, magic: int = 234000,
    ):
        self._mt5 = mt5_module
        self._login = login
        self._password = password
        self._server = server
        self._symbol_suffix = symbol_suffix
        self._path = path
        self._magic = magic
        # In-memory only -- see MT5UnknownOrderError's docstring and this
        # module's top-level docstring for the stated restart limitation.
        self._order_registry: dict[int, _OrderRecord] = {}
        self._recent_fills: list[WireFill] = []

    # --- connection lifecycle -------------------------------------------------

    def connect(self) -> None:
        kwargs: dict[str, Any] = {}
        if self._path:
            kwargs["path"] = self._path
        if self._login is not None:
            kwargs["login"] = self._login
        if self._password:
            kwargs["password"] = self._password
        if self._server:
            kwargs["server"] = self._server
        if not self._mt5.initialize(**kwargs):
            raise MT5ConnectionError(f"initialize() failed: {self._mt5.last_error()}")

    def disconnect(self) -> None:
        self._mt5.shutdown()

    def is_connected(self) -> bool:
        return self._mt5.account_info() is not None

    def is_live_account(self) -> bool:
        info = self._mt5.account_info()
        live_mode = getattr(self._mt5, "ACCOUNT_TRADE_MODE_REAL", None)
        return info is not None and live_mode is not None and info.trade_mode == live_mode

    def _require_account_info(self) -> Any:
        info = self._mt5.account_info()
        if info is None:
            raise MT5ConnectionError(f"account_info() returned None: {self._mt5.last_error()}")
        return info

    # --- account/positions -----------------------------------------------------

    def get_account_equity(self) -> float:
        return self._require_account_info().equity

    def get_free_margin(self) -> float:
        return self._require_account_info().margin_free

    def get_positions(self) -> list[WirePosition]:
        rows = self._mt5.positions_get() or ()
        positions = []
        for row in rows:
            side = "buy" if row.type == self._mt5.ORDER_TYPE_BUY else "sell"
            record = self._order_registry.get(getattr(row, "identifier", None))
            positions.append(WirePosition(
                symbol=mt5_symbol_to_wire_pair(row.symbol, suffix=self._symbol_suffix),
                side=side, quantity=row.volume, avg_entry_price=row.price_open,
                stop_price=row.sl or None, target_price=row.tp or None,
                trailing_stop_pips=None,
                opened_at=datetime.fromtimestamp(row.time, tz=timezone.utc),
                # A registry miss means either a manually-opened position
                # or one opened before this connector process's current
                # lifetime -- "external" is the same sentinel concept
                # fx_bot.ExitReason.EXTERNAL_CLOSE already anticipates for
                # exactly this gap, not a new workaround invented here.
                strategy_name=record.strategy_name if record else "external",
                entry_signal_id=record.signal_id if record else None,
                realized_pnl=0.0, max_favorable_excursion=0.0, max_adverse_excursion=0.0,
                partial_exit_taken=False, swap=row.swap,
            ))
        return positions

    # --- market data -------------------------------------------------------------

    def get_snapshot(self, symbol: str) -> WireMarketSnapshot:
        mt5_symbol = wire_pair_to_mt5_symbol(symbol, suffix=self._symbol_suffix)
        self._mt5.symbol_select(mt5_symbol, True)
        tick = self._mt5.symbol_info_tick(mt5_symbol)
        if tick is None:
            raise MT5SymbolError(f"No tick available for {mt5_symbol!r} ({self._mt5.last_error()}).")
        return WireMarketSnapshot(
            symbol=symbol, timestamp=datetime.fromtimestamp(tick.time, tz=timezone.utc),
            bid=tick.bid, ask=tick.ask,
        )

    def _resolve_timeframe(self, interval: str) -> Any:
        attr = _INTERVAL_TO_TIMEFRAME_ATTR.get(interval)
        if attr is None:
            raise MT5ClientError(f"Unsupported interval {interval!r}.")
        return getattr(self._mt5, attr)

    def get_bars(self, symbol: str, interval: str, lookback: int) -> list[WireMarketSnapshot]:
        mt5_symbol = wire_pair_to_mt5_symbol(symbol, suffix=self._symbol_suffix)
        timeframe = self._resolve_timeframe(interval)
        rates = self._mt5.copy_rates_from(mt5_symbol, timeframe, datetime.now(timezone.utc), lookback)
        if rates is None:
            raise MT5ClientError(f"copy_rates_from returned None for {mt5_symbol!r} ({self._mt5.last_error()}).")
        # WireMarketSnapshot has no OHLC fields and there's no verified
        # historical bid/ask API -- an explicit, lossy compromise: each
        # bar becomes a synthetic zero-spread snapshot at its close.
        # Revisit once fx_bot's own long-open "bar shape" question
        # resolves (see docs/ARCHITECTURE.md's Phase 3 write-up).
        snapshots = []
        for rate in rates:
            close = float(rate["close"])
            snapshots.append(WireMarketSnapshot(
                symbol=symbol, timestamp=datetime.fromtimestamp(int(rate["time"]), tz=timezone.utc),
                bid=close, ask=close,
            ))
        return snapshots

    # --- orders --------------------------------------------------------------

    def place_order(self, wire_order: WireOrder) -> WireOrder:
        if wire_order.order_type == "trailing_stop":
            # No native MT5 order-type equivalent -- fx_bot implements
            # trailing via PositionManager tightening stop_loss_price
            # through modify_order instead, and OrderManager today only
            # ever builds MARKET entries, so this should be unreachable
            # in practice. Raising defensively rather than guessing at a
            # mapping that doesn't exist.
            raise MT5ClientError(
                "order_type 'trailing_stop' has no native MT5 mapping; "
                "trail stop_loss_price via modify_order instead."
            )

        mt5_symbol = wire_pair_to_mt5_symbol(wire_order.symbol, suffix=self._symbol_suffix)
        self._mt5.symbol_select(mt5_symbol, True)

        request: dict[str, Any] = {
            "symbol": mt5_symbol,
            "volume": wire_order.quantity,
            "magic": self._magic,
            "comment": _order_comment(wire_order.client_order_id),
        }
        if wire_order.stop_loss_price is not None:
            request["sl"] = wire_order.stop_loss_price
        if wire_order.take_profit_price is not None:
            request["tp"] = wire_order.take_profit_price

        if wire_order.order_type == "market":
            tick = self._mt5.symbol_info_tick(mt5_symbol)
            if tick is None:
                raise MT5SymbolError(f"No tick available for {mt5_symbol!r}.")
            request["action"] = self._mt5.TRADE_ACTION_DEAL
            request["type"] = getattr(self._mt5, _MARKET_ORDER_TYPE_ATTR[wire_order.side])
            request["price"] = tick.ask if wire_order.side == "buy" else tick.bid
            filling_attr = _FILLING_ATTR_FOR_TIF.get(wire_order.time_in_force, "ORDER_FILLING_RETURN")
            request["type_filling"] = getattr(self._mt5, filling_attr)
        elif wire_order.order_type in ("limit", "stop", "stop_limit"):
            request["action"] = self._mt5.TRADE_ACTION_PENDING
            if wire_order.order_type == "limit":
                request["type"] = getattr(self._mt5, _LIMIT_ORDER_TYPE_ATTR[wire_order.side])
                request["price"] = wire_order.limit_price
            elif wire_order.order_type == "stop":
                request["type"] = getattr(self._mt5, _STOP_ORDER_TYPE_ATTR[wire_order.side])
                request["price"] = wire_order.stop_price
            else:
                request["type"] = getattr(self._mt5, _STOP_LIMIT_ORDER_TYPE_ATTR[wire_order.side])
                request["price"] = wire_order.stop_price
                request["stoplimit"] = wire_order.limit_price
            # IOC/FOK have no pending-order-expiration equivalent --
            # treated as GTC, a stated limitation pending 5g's real
            # per-broker type_filling/type_time support confirmation.
            request["type_time"] = getattr(
                self._mt5, "ORDER_TIME_DAY" if wire_order.time_in_force == "day" else "ORDER_TIME_GTC",
            )
        else:
            raise MT5ClientError(f"Unknown order_type {wire_order.order_type!r}.")

        result = self._mt5.order_send(request)
        if result is None:
            raise MT5ClientError(f"order_send returned None: {self._mt5.last_error()}")
        if result.retcode != self._mt5.TRADE_RETCODE_DONE:
            raise MT5OrderRejectedError(
                result.retcode, getattr(result, "comment", ""),
                error_type=_RETCODE_ERROR_TYPES.get(result.retcode, "OrderRejected"),
            )

        now = datetime.now(timezone.utc)
        filled = WireOrder(
            symbol=wire_order.symbol, side=wire_order.side, order_type=wire_order.order_type,
            quantity=result.volume or wire_order.quantity, time_in_force=wire_order.time_in_force,
            limit_price=wire_order.limit_price, stop_price=wire_order.stop_price,
            stop_loss_price=wire_order.stop_loss_price, take_profit_price=wire_order.take_profit_price,
            trailing_pips=wire_order.trailing_pips, exit_reason=wire_order.exit_reason,
            status="filled" if wire_order.order_type == "market" else "accepted",
            client_order_id=wire_order.client_order_id, broker_order_id=str(result.order),
            created_at=now, updated_at=now,
            strategy_name=wire_order.strategy_name, signal_id=wire_order.signal_id,
        )
        self._order_registry[result.order] = _OrderRecord(
            client_order_id=wire_order.client_order_id, strategy_name=wire_order.strategy_name,
            signal_id=wire_order.signal_id, wire_order=filled,
        )
        if wire_order.order_type == "market":
            # Synthesized directly from order_send's own result rather
            # than reaching for history_deals_get/history_orders_get --
            # those aren't in the confirmed API surface (see this
            # module's docstring). Limitation, stated plainly: this only
            # ever reflects fills THIS connector produced via
            # place_order, within the current process's lifetime.
            self._recent_fills.append(WireFill(
                order_client_id=str(result.order), symbol=wire_order.symbol, side=wire_order.side,
                quantity=result.volume or wire_order.quantity, price=result.price, filled_at=now, fees=0.0,
            ))
        return filled

    def cancel_order(self, broker_order_id: str) -> None:
        ticket = _parse_ticket(broker_order_id)
        action = getattr(self._mt5, "TRADE_ACTION_REMOVE", None)
        if action is None:
            raise MT5ClientError("MetaTrader5 module has no TRADE_ACTION_REMOVE constant.")
        result = self._mt5.order_send({"action": action, "order": ticket})
        retcode = getattr(result, "retcode", None)
        if result is None or retcode != self._mt5.TRADE_RETCODE_DONE:
            raise MT5OrderRejectedError(
                retcode if retcode is not None else -1, getattr(result, "comment", ""),
                error_type=_RETCODE_ERROR_TYPES.get(retcode, "OrderRejected"),
            )

    def modify_order(self, broker_order_id: str, changes: dict) -> WireOrder:
        ticket = _parse_ticket(broker_order_id)
        record = self._order_registry.get(ticket)
        if record is None:
            raise MT5UnknownOrderError(f"No known order with broker_order_id {broker_order_id!r}.")

        new_sl = changes.get("stop_loss_price", record.wire_order.stop_loss_price)
        new_tp = changes.get("take_profit_price", record.wire_order.take_profit_price)

        # An open position (TRADE_ACTION_SLTP, ticket = the POSITION
        # ticket) is a different request shape from a still-pending
        # order (TRADE_ACTION_MODIFY, ticket = the ORDER ticket) --
        # MT5's identifier field links a position back to the order
        # ticket that created it, so that's what's searched here.
        positions = self._mt5.positions_get() or ()
        position = next((p for p in positions if getattr(p, "identifier", None) == ticket), None)

        if position is not None:
            request: dict[str, Any] = {
                "action": self._mt5.TRADE_ACTION_SLTP, "position": position.ticket,
                "symbol": position.symbol, "sl": new_sl or 0.0, "tp": new_tp or 0.0,
            }
        else:
            request = {"action": self._mt5.TRADE_ACTION_MODIFY, "order": ticket, "sl": new_sl or 0.0, "tp": new_tp or 0.0}
            if "limit_price" in changes:
                request["price"] = changes["limit_price"]
            elif "stop_price" in changes:
                request["price"] = changes["stop_price"]

        result = self._mt5.order_send(request)
        retcode = getattr(result, "retcode", None)
        if result is None or retcode != self._mt5.TRADE_RETCODE_DONE:
            raise MT5OrderRejectedError(
                retcode if retcode is not None else -1, getattr(result, "comment", ""),
                error_type=_RETCODE_ERROR_TYPES.get(retcode, "OrderRejected"),
            )

        updated = record.wire_order.model_copy(
            update={"stop_loss_price": new_sl, "take_profit_price": new_tp, "updated_at": datetime.now(timezone.utc)},
        )
        self._order_registry[ticket] = _OrderRecord(
            record.client_order_id, record.strategy_name, record.signal_id, updated,
        )
        return updated

    def get_order_status(self, broker_order_id: str) -> WireOrder:
        ticket = _parse_ticket(broker_order_id)
        record = self._order_registry.get(ticket)
        if record is None:
            raise MT5UnknownOrderError(f"No known order with broker_order_id {broker_order_id!r}.")
        return record.wire_order

    def poll_fills(self, since: Optional[str]) -> list[WireFill]:
        if since is None:
            return list(self._recent_fills)
        since_dt = datetime.fromisoformat(since)
        return [f for f in self._recent_fills if f.filled_at > since_dt]
