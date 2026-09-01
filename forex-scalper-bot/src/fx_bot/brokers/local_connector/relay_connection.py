"""
RelayConnection -- the cloud side's handle to one connected MT4/5
connector socket. Owns a correlation-id-keyed pending-call table and
presents a fully synchronous, blocking facade (send_request) so
LocalConnectorBroker can be a drop-in BrokerClient for entirely
synchronous callers (RiskEngine, OrderManager, BacktestEngine), even
though the actual socket I/O runs on a shared background asyncio event
loop (see relay_server.py's RelayServer, which owns that loop and hands
out one RelayConnection per accepted connector).

Concurrency model, chosen deliberately over one-thread-per-connection:
every RelayConnection is scheduled onto ONE shared event loop via
asyncio.run_coroutine_threadsafe, so this scales to many simultaneous
connectors (Phase 10's eventual multi-tenant world) without a rewrite --
see the approved Phase 5b design for the full rationale. Every method
that touches `_pending`/`_read_loop` internals runs as a coroutine on
that shared loop, which makes `_pending` safe with no lock (asyncio
guarantees single-threaded coroutine execution); `_event_handlers` is
touched from arbitrary calling threads (subscribe_quotes et al) as well
as the loop thread, so it gets its own small lock.

`_transport` only needs to structurally satisfy async `send(str)`,
`recv() -> str`, `close(code=, reason=)` -- a real `websockets`
connection already does, with no wrapper needed; a future FastAPI
`WebSocket` adapter would just need to expose the same three methods.

Phase 5c note: authentication (`_authenticate`) is a separate, one-time
handshake that runs BEFORE `_read_loop` ever starts -- see
relay_server.py's `_handle_connection`, which awaits `_authenticate`
first and only proceeds to queue the connection and start `_read_loop`
on success. That ordering, not anything in this class alone, is what
guarantees `RelayServer.accept()` only ever returns an authenticated
connection.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional, Protocol

from websockets.exceptions import ConnectionClosed

from relay_protocol.envelope import Envelope, EnvelopeKind

from .exceptions import BrokerRejectedError, ConnectorOfflineError, ConnectorTimeoutError

logger = logging.getLogger(__name__)

# WebSocket close code used to tell a connector its auth attempt failed --
# distinct from an ordinary close so the connector knows to stop
# auto-reconnecting and prompt for re-pairing instead of hot-looping.
AUTH_FAILURE_CLOSE_CODE = 4401


class _Transport(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str: ...
    async def close(self, code: int = 1000, reason: str = "") -> None: ...


class RelayConnection:
    def __init__(self, loop: asyncio.AbstractEventLoop, transport: _Transport, *, default_timeout: float = 5.0):
        self._loop = loop
        self._transport = transport
        self._default_timeout = default_timeout
        self._pending: dict[str, asyncio.Future] = {}
        self._event_handlers: dict[str, list[Callable[[dict], None]]] = {}
        self._handlers_lock = threading.Lock()
        self._connected = threading.Event()
        self._connected.set()
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="relay-event")
        # Set by a successful _authenticate() handshake -- None until then.
        self.account_id: Optional[str] = None

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    async def _authenticate(self, authenticator: Callable[[str], Optional[str]], *, grace_seconds: float) -> bool:
        """Runs once, before _read_loop ever starts (see relay_server.py's
        _handle_connection). Returns True (self.account_id set) iff a
        valid `auth` envelope arrived within grace_seconds; otherwise the
        socket is already closed (with AUTH_FAILURE_CLOSE_CODE where a
        peer is still there to receive it) and this returns False.

        The grace-period timeout matters as much as the "first frame must
        be auth" rule below -- without it, a connector that opens a
        socket and never sends anything would tie up this coroutine (and
        the socket) forever."""
        try:
            raw = await asyncio.wait_for(self._transport.recv(), timeout=grace_seconds)
        except (asyncio.TimeoutError, ConnectionClosed):
            await self._close_quietly(code=AUTH_FAILURE_CLOSE_CODE, reason="No auth frame received in time.")
            return False

        try:
            envelope = Envelope.from_wire(raw)
        except Exception:
            await self._reject(None, "AuthenticationFailed", "First frame must be a valid `auth` envelope.")
            return False

        if envelope.kind != EnvelopeKind.AUTH:
            await self._reject(envelope.id, "AuthenticationFailed", "First frame on a new connection must be `auth`.")
            return False

        token = envelope.payload.get("token", "")
        # A sync sqlite lookup must never block the ONE shared event loop
        # every other RelayConnection also depends on -- same reasoning
        # already applied to event-handler dispatch via ThreadPoolExecutor.
        account_id = await self._loop.run_in_executor(None, authenticator, token)
        if account_id is None:
            await self._reject(envelope.id, "AuthenticationFailed", "Invalid or unrecognized token.")
            return False

        self.account_id = account_id
        try:
            await self._transport.send(Envelope.make_response(envelope.id, "auth", {"account_id": account_id}).to_wire())
        except ConnectionClosed:
            return False  # peer vanished between validating and acking
        return True

    async def _reject(self, request_id: Optional[str], error_type: str, message: str) -> None:
        try:
            if request_id is not None:
                await self._transport.send(
                    Envelope.make_error(request_id, "auth", error_type=error_type, message=message).to_wire()
                )
        except ConnectionClosed:
            pass
        await self._close_quietly(code=AUTH_FAILURE_CLOSE_CODE, reason=message)

    async def _close_quietly(self, *, code: int = 1000, reason: str = "") -> None:
        try:
            await self._transport.close(code=code, reason=reason)
        except Exception:
            pass

    def send_request(self, method: str, payload: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        """Blocks the calling thread. Raises ConnectorOfflineError,
        ConnectorTimeoutError, or BrokerRejectedError; returns the raw
        response payload dict on success."""
        if not self.is_connected:
            raise ConnectorOfflineError(f"Not connected; cannot send {method!r}.")
        wait_seconds = timeout if timeout is not None else self._default_timeout
        future = asyncio.run_coroutine_threadsafe(self._send_and_wait(method, payload, wait_seconds), self._loop)
        # A small grace beyond the inner asyncio.wait_for deadline --
        # the coroutine itself never runs longer than wait_seconds, this
        # just avoids a razor-thin race waiting on the outer future.
        return future.result(timeout=wait_seconds + 1.0)

    async def _send_and_wait(self, method: str, payload: dict[str, Any], wait_seconds: float) -> dict[str, Any]:
        envelope = Envelope.make_request(method, payload)
        loop_future: asyncio.Future = self._loop.create_future()
        self._pending[envelope.id] = loop_future
        try:
            await self._transport.send(envelope.to_wire())
        except ConnectionClosed as exc:
            self._pending.pop(envelope.id, None)
            raise ConnectorOfflineError(f"Connection closed while sending {method!r}.") from exc
        try:
            return await asyncio.wait_for(loop_future, timeout=wait_seconds)
        except asyncio.TimeoutError as exc:
            self._pending.pop(envelope.id, None)
            raise ConnectorTimeoutError(f"No response to {method!r} within {wait_seconds}s.") from exc

    async def _read_loop(self) -> None:
        """Runs for the connection's entire lifetime -- see relay_server.py's
        connection handler, which awaits this directly (its own coroutine
        IS this read loop) since `websockets` closes a connection the
        instant its handler returns."""
        try:
            while True:
                try:
                    raw = await self._transport.recv()
                except ConnectionClosed:
                    break
                self._handle_frame(raw)
        finally:
            self._connected.clear()
            self._fail_all_pending()
            self._executor.shutdown(wait=False)

    def _handle_frame(self, raw: str) -> None:
        envelope = Envelope.from_wire(raw)
        if envelope.kind == EnvelopeKind.RESPONSE:
            future = self._pending.pop(envelope.id, None)
            if future is not None and not future.done():
                future.set_result(envelope.payload)
        elif envelope.kind == EnvelopeKind.ERROR:
            future = self._pending.pop(envelope.id, None)
            if future is not None and not future.done():
                error_type = envelope.payload.get("error_type", "UnknownError")
                message = envelope.payload.get("message", "")
                future.set_exception(BrokerRejectedError(error_type, message))
        elif envelope.kind == EnvelopeKind.EVENT:
            self._dispatch_event(envelope.method, envelope.payload)
        elif envelope.kind == EnvelopeKind.AUTH:
            # The FIRST auth frame is consumed exclusively by
            # _authenticate() before _read_loop (and so _handle_frame)
            # ever runs -- reaching here means a SECOND, late/duplicate
            # auth frame arrived on an already-authenticated connection
            # (a misbehaving connector, not a security event on its own).
            # Logged and ignored rather than silently vanished or treated
            # as a re-authentication attempt.
            logger.warning(
                "Received a duplicate/late auth frame on an already-authenticated "
                "connection (account_id=%s); ignoring.", self.account_id,
            )
        # A bare REQUEST frame from the connector has no defined meaning
        # yet (the connector never initiates a request in this protocol)
        # and is silently ignored, same as before Phase 5c.

    def _dispatch_event(self, method: str, payload: dict[str, Any]) -> None:
        with self._handlers_lock:
            handlers = list(self._event_handlers.get(method, ()))
        for handler in handlers:
            # Off the loop thread, so one slow/blocking on_update callback
            # can never stall this connection's ability to keep resolving
            # other pending requests.
            self._executor.submit(handler, payload)

    def _fail_all_pending(self) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(ConnectorOfflineError("Connection closed."))
        self._pending.clear()

    def register_event_handler(self, method: str, callback: Callable[[dict], None]) -> None:
        with self._handlers_lock:
            self._event_handlers.setdefault(method, []).append(callback)

    def unregister_event_handler(self, method: str, callback: Callable[[dict], None]) -> None:
        with self._handlers_lock:
            handlers = self._event_handlers.get(method, [])
            if callback in handlers:
                handlers.remove(callback)

    def close(self, code: int = 1000, reason: str = "") -> None:
        async def _close() -> None:
            await self._transport.close(code=code, reason=reason)

        try:
            asyncio.run_coroutine_threadsafe(_close(), self._loop).result(timeout=5.0)
        except Exception:
            pass
