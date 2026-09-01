"""
BrokerClient implementation that talks to a user's local MT4/5 connector
over the relay protocol (see relay_protocol/ and this project's Phase 5
design docs). Every method is a synchronous send_request/wire_convert
round trip through a RelayConnection -- see that class for the actual
async-internals-behind-a-sync-facade machinery. `ConnectorOfflineError`/
`ConnectorTimeoutError`/`BrokerRejectedError` all propagate untouched out
of every method -- nothing here ever catches and downgrades them, same
"never fold a failure into a quiet default" discipline PaperBrokerClient
follows for its own NotImplementedError/KeyError cases.

Phase 5b scope only: no auth/pairing exists yet (that's Phase 5c), so
`connect()` only asserts an already-connected relay rather than dialing
anything, and `is_live` is a plain constructor flag rather than a value
read off a real MT5 account-type field -- relay_protocol has no `hello`/
auth-ack frame carrying that yet. Wiring `is_live` to a real handshake
value is deferred to 5c, not guessed at now (see the approved Phase 5b
design's resolution of its own open question #1).
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from relay_protocol.methods import EventMethod, RequestMethod
from relay_protocol.wire_models import WireFill, WireMarketSnapshot, WireOrder, WirePosition

from ...interfaces.broker import BrokerClient
from ...models import Fill, MarketSnapshot, Order, Position
from .exceptions import ConnectorOfflineError
from .relay_connection import RelayConnection
from .wire_convert import (
    order_to_wire_order,
    wire_fill_to_fill,
    wire_order_to_order,
    wire_position_to_position,
    wire_snapshot_to_snapshot,
)


class LocalConnectorBroker(BrokerClient):
    def __init__(self, relay: RelayConnection, *, request_timeout: float = 5.0, is_live: bool = False):
        self._relay = relay
        self._request_timeout = request_timeout
        self._is_live = is_live
        # Keyed by frozenset(symbols) to match a later unsubscribe_quotes
        # call to the subscribe_quotes call that registered it -- the ABC
        # gives unsubscribe_quotes no callback reference to match against
        # directly, only the symbol list, so this is the only matching key
        # available. Order-independent (frozenset, not the raw list) so a
        # caller doesn't have to pass symbols back in the exact same order.
        self._quote_handlers: dict[frozenset, Callable[[dict], None]] = {}
        self._last_known_positions: Optional[tuple[list[Position], datetime]] = None

    def connect(self) -> None:
        if not self._relay.is_connected:
            raise ConnectorOfflineError("Relay connection is not connected.")

    def disconnect(self) -> None:
        self._relay.close()

    def get_account_equity(self) -> float:
        payload = self._relay.send_request(RequestMethod.GET_ACCOUNT_EQUITY, {}, timeout=self._request_timeout)
        return payload["equity"]

    def get_free_margin(self) -> float:
        payload = self._relay.send_request(RequestMethod.GET_FREE_MARGIN, {}, timeout=self._request_timeout)
        return payload["free_margin"]

    def get_positions(self) -> list[Position]:
        payload = self._relay.send_request(RequestMethod.GET_POSITIONS, {}, timeout=self._request_timeout)
        positions = [wire_position_to_position(WirePosition(**p)) for p in payload["positions"]]
        self._last_known_positions = (positions, datetime.utcnow())
        return positions

    def get_last_known_positions(self) -> tuple[list[Position], datetime]:
        """Display/alerting accessor only -- see this class's docstring
        and the approved Phase 5b design: never consumed by
        RiskEngine/PositionManager, which must only ever see a live read
        (or an explicit failure) from get_positions(), never a stale
        cache. Deliberately minimal for 5b: just the last successful
        read, no polling/proactive refresh/staleness-alert thresholds --
        that full two-tier system is Phase 5f."""
        if self._last_known_positions is None:
            raise RuntimeError("get_positions() has never succeeded yet -- nothing cached.")
        return self._last_known_positions

    def get_snapshot(self, symbol: str) -> MarketSnapshot:
        payload = self._relay.send_request(
            RequestMethod.GET_SNAPSHOT, {"symbol": symbol}, timeout=self._request_timeout,
        )
        return wire_snapshot_to_snapshot(WireMarketSnapshot(**payload))

    def get_bars(self, symbol: str, interval: str, lookback: int) -> list[MarketSnapshot]:
        payload = self._relay.send_request(
            RequestMethod.GET_BARS, {"symbol": symbol, "interval": interval, "lookback": lookback},
            timeout=self._request_timeout,
        )
        return [wire_snapshot_to_snapshot(WireMarketSnapshot(**bar)) for bar in payload["bars"]]

    def subscribe_quotes(self, symbols: list[str], on_update: Callable[[MarketSnapshot], None]) -> None:
        symbol_set = frozenset(symbols)

        def _handler(payload: dict) -> None:
            if payload.get("symbol") not in symbol_set:
                return
            on_update(wire_snapshot_to_snapshot(WireMarketSnapshot(**payload)))

        self._quote_handlers[symbol_set] = _handler
        self._relay.register_event_handler(EventMethod.QUOTE, _handler)
        self._relay.send_request(RequestMethod.SUBSCRIBE_QUOTES, {"symbols": symbols}, timeout=self._request_timeout)

    def unsubscribe_quotes(self, symbols: list[str]) -> None:
        handler = self._quote_handlers.pop(frozenset(symbols), None)
        if handler is not None:
            self._relay.unregister_event_handler(EventMethod.QUOTE, handler)
        self._relay.send_request(RequestMethod.UNSUBSCRIBE_QUOTES, {"symbols": symbols}, timeout=self._request_timeout)

    def place_order(self, order: Order) -> Order:
        wire = order_to_wire_order(order)
        payload = self._relay.send_request(
            RequestMethod.PLACE_ORDER, wire.model_dump(mode="json"), timeout=self._request_timeout,
        )
        return wire_order_to_order(WireOrder(**payload))

    def cancel_order(self, broker_order_id: str) -> None:
        self._relay.send_request(
            RequestMethod.CANCEL_ORDER, {"broker_order_id": broker_order_id}, timeout=self._request_timeout,
        )

    def modify_order(self, broker_order_id: str, **changes) -> Order:
        payload = self._relay.send_request(
            RequestMethod.MODIFY_ORDER, {"broker_order_id": broker_order_id, "changes": changes},
            timeout=self._request_timeout,
        )
        return wire_order_to_order(WireOrder(**payload))

    def get_order_status(self, broker_order_id: str) -> Order:
        payload = self._relay.send_request(
            RequestMethod.GET_ORDER_STATUS, {"broker_order_id": broker_order_id}, timeout=self._request_timeout,
        )
        return wire_order_to_order(WireOrder(**payload))

    def poll_fills(self, since: Optional[datetime] = None) -> list[Fill]:
        payload = self._relay.send_request(
            RequestMethod.POLL_FILLS, {"since": since.isoformat() if since else None}, timeout=self._request_timeout,
        )
        return [wire_fill_to_fill(WireFill(**f)) for f in payload["fills"]]

    @property
    def is_live(self) -> bool:
        return self._is_live
