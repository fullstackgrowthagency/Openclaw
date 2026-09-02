"""
RelayClient -- the connector-side counterpart to the cloud's
RelayConnection/RelayServer, but as a WebSocket CLIENT with real MT5
dispatch instead of test scripting. See docs/ARCHITECTURE.md's Phase 5d
section (main forex-scalper-bot project) for the full design.

Only the cloud ever sends `request` frames in this protocol; this class
only ever replies with `response`/`error`, or pushes `event` frames
unprompted (quotes/heartbeat/mt5_disconnected/mt5_reconnected) -- a bare
`request` frame has no defined meaning coming the other way and is
ignored here, same discipline the cloud's own RelayConnection applies to
a stray REQUEST frame from a connector.

Auth ordering deliberately mirrors the CLOUD's RelayConnection
(_authenticate before _read_loop even starts), not FakeRelayPeer's own
pattern (which needs an id-correlation table because ITS read loop is
already running when it authenticates) -- nothing else is reading this
socket yet when `_authenticate` runs, so a single direct `recv()` for
the ack is sufficient.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

import websockets.asyncio.client as ws_client
from websockets.exceptions import ConnectionClosed

from relay_protocol.envelope import AUTH_FAILURE_CLOSE_CODE, Envelope, EnvelopeKind
from relay_protocol.methods import EventMethod, RequestMethod
from relay_protocol.wire_models import WireOrder

from .backoff import backoff_delay
from .mt5_client import MT5Client, MT5ClientError
from .mt5_executor import MT5Executor

logger = logging.getLogger(__name__)

# A connection must stay authenticated and open this long before a
# reconnect attempt resets the backoff counter to 0 -- resetting on
# every successful auth (rather than a genuinely STABLE session) would
# let a connection that auths and immediately drops every time hot-loop
# at ~1s forever instead of backing off properly.
_STABLE_SECONDS = 10.0


class AuthFailure(Exception):
    """Raised on a rejected/expired token (an `error` reply and/or a
    WebSocket close with AUTH_FAILURE_CLOSE_CODE). Callers MUST NOT
    auto-reconnect on this -- re-pairing is required."""


class RelayClient:
    def __init__(
        self, url: str, *, token: str, account_id: str,
        mt5_client: MT5Client, mt5_executor: MT5Executor,
        request_timeout: float = 5.0, backoff_base: float = 1.0, backoff_cap: float = 60.0,
    ):
        self._url = url
        self._token = token
        self._account_id = account_id
        self._mt5 = mt5_client
        self._mt5x = mt5_executor
        self._request_timeout = request_timeout
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._ws = None
        self._dispatch: dict[str, Callable[[dict], Awaitable[dict]]] = self._build_dispatch_table()
        self._subscribed_symbols: set[str] = set()

    @property
    def subscribed_symbols(self) -> frozenset:
        return frozenset(self._subscribed_symbols)

    def update_credentials(self, token: str, account_id: str) -> None:
        self._token = token
        self._account_id = account_id

    # --- auth --------------------------------------------------------------

    async def _authenticate(self) -> None:
        await self._ws.send(Envelope.make_auth(token=self._token, account_id=self._account_id).to_wire())
        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
        except (asyncio.TimeoutError, ConnectionClosed) as exc:
            if getattr(self._ws, "close_code", None) == AUTH_FAILURE_CLOSE_CODE:
                raise AuthFailure("Relay closed the connection during auth (4401).") from exc
            raise
        envelope = Envelope.from_wire(raw)
        if envelope.kind == EnvelopeKind.ERROR:
            raise AuthFailure(f"{envelope.payload.get('error_type')}: {envelope.payload.get('message')}")
        if envelope.kind != EnvelopeKind.RESPONSE or envelope.method != "auth":
            raise AuthFailure(f"Unexpected first frame: {envelope.kind}/{envelope.method}")

    # --- dispatch ------------------------------------------------------------

    def _build_dispatch_table(self) -> dict[str, Callable[[dict], Awaitable[dict]]]:
        return {
            RequestMethod.GET_ACCOUNT_EQUITY: self._handle_get_account_equity,
            RequestMethod.GET_FREE_MARGIN: self._handle_get_free_margin,
            RequestMethod.GET_POSITIONS: self._handle_get_positions,
            RequestMethod.GET_SNAPSHOT: self._handle_get_snapshot,
            RequestMethod.GET_BARS: self._handle_get_bars,
            RequestMethod.SUBSCRIBE_QUOTES: self._handle_subscribe_quotes,
            RequestMethod.UNSUBSCRIBE_QUOTES: self._handle_unsubscribe_quotes,
            RequestMethod.PLACE_ORDER: self._handle_place_order,
            RequestMethod.CANCEL_ORDER: self._handle_cancel_order,
            RequestMethod.MODIFY_ORDER: self._handle_modify_order,
            RequestMethod.GET_ORDER_STATUS: self._handle_get_order_status,
            RequestMethod.POLL_FILLS: self._handle_poll_fills,
        }

    async def _read_loop(self) -> None:
        async for raw in self._ws:
            await self._handle_frame(raw)

    async def _handle_frame(self, raw: str) -> None:
        try:
            envelope = Envelope.from_wire(raw)
        except Exception:
            logger.warning("Ignoring malformed frame from relay.")
            return
        if envelope.kind != EnvelopeKind.REQUEST:
            return  # only the cloud ever sends requests in this protocol
        handler = self._dispatch.get(envelope.method)
        if handler is None:
            await self._send_error(envelope.id, envelope.method, "UnknownMethod", f"No handler for {envelope.method!r}.")
            return
        try:
            payload = await handler(envelope.payload)
        except MT5ClientError as exc:
            await self._send_error(envelope.id, envelope.method, getattr(exc, "error_type", type(exc).__name__), str(exc))
        except Exception as exc:
            logger.exception("Unhandled error in handler for %s", envelope.method)
            await self._send_error(envelope.id, envelope.method, "InternalConnectorError", str(exc))
        else:
            await self._safe_send(Envelope.make_response(envelope.id, envelope.method, payload))

    async def _send_error(self, request_id: Optional[str], method: str, error_type: str, message: str) -> None:
        await self._safe_send(Envelope.make_error(request_id, method, error_type=error_type, message=message))

    # --- individual handlers -- thin closures routing through MT5Executor -------

    async def _handle_get_account_equity(self, payload: dict) -> dict:
        return {"equity": await self._mt5x.run(self._mt5.get_account_equity)}

    async def _handle_get_free_margin(self, payload: dict) -> dict:
        return {"free_margin": await self._mt5x.run(self._mt5.get_free_margin)}

    async def _handle_get_positions(self, payload: dict) -> dict:
        positions = await self._mt5x.run(self._mt5.get_positions)
        return {"positions": [p.model_dump(mode="json") for p in positions]}

    async def _handle_get_snapshot(self, payload: dict) -> dict:
        snapshot = await self._mt5x.run(self._mt5.get_snapshot, payload["symbol"])
        return snapshot.model_dump(mode="json")

    async def _handle_get_bars(self, payload: dict) -> dict:
        bars = await self._mt5x.run(self._mt5.get_bars, payload["symbol"], payload["interval"], payload["lookback"])
        return {"bars": [b.model_dump(mode="json") for b in bars]}

    async def _handle_subscribe_quotes(self, payload: dict) -> dict:
        self._subscribed_symbols.update(payload.get("symbols", []))
        return {}

    async def _handle_unsubscribe_quotes(self, payload: dict) -> dict:
        self._subscribed_symbols.difference_update(payload.get("symbols", []))
        return {}

    async def _handle_place_order(self, payload: dict) -> dict:
        order = await self._mt5x.run(self._mt5.place_order, WireOrder(**payload))
        return order.model_dump(mode="json")

    async def _handle_cancel_order(self, payload: dict) -> dict:
        await self._mt5x.run(self._mt5.cancel_order, payload["broker_order_id"])
        return {}

    async def _handle_modify_order(self, payload: dict) -> dict:
        order = await self._mt5x.run(self._mt5.modify_order, payload["broker_order_id"], payload["changes"])
        return order.model_dump(mode="json")

    async def _handle_get_order_status(self, payload: dict) -> dict:
        order = await self._mt5x.run(self._mt5.get_order_status, payload["broker_order_id"])
        return order.model_dump(mode="json")

    async def _handle_poll_fills(self, payload: dict) -> dict:
        fills = await self._mt5x.run(self._mt5.poll_fills, payload.get("since"))
        return {"fills": [f.model_dump(mode="json") for f in fills]}

    # --- event pushing (one-way, no ack expected) -------------------------------

    async def push_quote(self, snapshot) -> None:
        await self._safe_send(Envelope.make_event(EventMethod.QUOTE, snapshot.model_dump(mode="json")))

    async def push_heartbeat(self, payload: Optional[dict] = None) -> None:
        await self._safe_send(Envelope.make_event(EventMethod.HEARTBEAT, payload or {}))

    async def push_mt5_disconnected(self, reason: str) -> None:
        await self._safe_send(Envelope.make_event(EventMethod.MT5_DISCONNECTED, {"reason": reason}))

    async def push_mt5_reconnected(self) -> None:
        await self._safe_send(Envelope.make_event(EventMethod.MT5_RECONNECTED, {}))

    async def _safe_send(self, envelope: Envelope) -> None:
        if self._ws is None:
            return  # not currently connected -- the next tick's push catches up post-reconnect
        try:
            await self._ws.send(envelope.to_wire())
        except ConnectionClosed:
            pass

    # --- connection lifecycle / reconnect-with-backoff --------------------------

    async def _safe_close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def run_forever(self) -> None:
        """Connects, authenticates, and serves requests until the socket
        drops, then reconnects with backoff -- forever, UNLESS the drop
        was an AuthFailure, which propagates to the caller instead (see
        this module's docstring and AuthFailure's own docstring: a
        rejected token can never succeed by retrying, only by
        re-pairing)."""
        attempt = 0
        while True:
            connected_at = time.monotonic()
            authed = False
            try:
                self._ws = await ws_client.connect(self._url)
                try:
                    await self._authenticate()
                    authed = True
                    await self._read_loop()
                finally:
                    await self._safe_close()
            except AuthFailure:
                raise
            except Exception as exc:
                logger.warning("Relay connection error: %s", exc)
            if authed and (time.monotonic() - connected_at) >= _STABLE_SECONDS:
                attempt = 0
            else:
                attempt += 1
            await asyncio.sleep(backoff_delay(attempt, base=self._backoff_base, cap=self._backoff_cap))
