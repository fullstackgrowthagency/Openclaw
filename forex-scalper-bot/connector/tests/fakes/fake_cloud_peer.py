"""
FakeCloudPeer -- a real websockets SERVER playing the cloud's role, for
testing RelayClient (the inverse of tests/fakes/fake_mt5_module.py, which
fakes MT5 instead of the cloud). Accepts one connector connection,
validates or scripts-rejects its `auth` frame, and lets a test drive a
request/response round trip (`send_request`) or read a connector-pushed
event (`read_pushed_event`) -- a real socket, not an in-process mock, for
the same reason the main forex-scalper-bot project's
tests/fakes/fake_relay_peer.py gives: it's the only way to exercise real
framing bugs.

Mirrors RelayServer's own run_forever-loop-lifecycle idiom (never tying
the loop's life to one coroutine) and FakeRelayPeer's idempotent stop().
"""
from __future__ import annotations

import asyncio
import queue
import threading
from typing import Optional

import websockets.asyncio.server as ws_server
from websockets.exceptions import ConnectionClosed

from relay_protocol.envelope import AUTH_FAILURE_CLOSE_CODE, Envelope, EnvelopeKind


class FakeCloudPeer:
    def __init__(
        self, *, expected_token: str = "test-token", expected_account_id: str = "test-account",
        reject_auth: bool = False,
    ):
        self._expected_token = expected_token
        self._expected_account_id = expected_account_id
        self._reject_auth = reject_auth
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._server = None
        self._port: Optional[int] = None
        self._ws = None
        self._connected = threading.Event()
        self._pending: dict[str, asyncio.Future] = {}
        self._events: "queue.Queue[Envelope]" = queue.Queue()
        self._stopped = False
        self.received_auth: Optional[Envelope] = None

    def start(self, host: str = "127.0.0.1", port: int = 0) -> str:
        """Starts listening and returns the ws:// URL immediately --
        does NOT block for a connection (unlike send_request/etc, which
        wait for one lazily when first called)."""
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self._start_serving(host, port), self._loop).result(timeout=5.0)
        return f"ws://{host}:{self._port}"

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _start_serving(self, host: str, port: int) -> None:
        self._server = await ws_server.serve(self._handle_connection, host, port)
        self._port = self._server.sockets[0].getsockname()[1]

    async def _handle_connection(self, websocket) -> None:
        self._ws = websocket
        try:
            raw = await websocket.recv()
        except ConnectionClosed:
            return
        try:
            envelope = Envelope.from_wire(raw)
        except Exception:
            await websocket.close(code=AUTH_FAILURE_CLOSE_CODE, reason="malformed auth frame")
            return
        self.received_auth = envelope
        valid = (
            not self._reject_auth
            and envelope.kind == EnvelopeKind.AUTH
            and envelope.payload.get("token") == self._expected_token
        )
        if not valid:
            await websocket.send(Envelope.make_error(
                envelope.id, "auth", error_type="AuthenticationFailed", message="rejected",
            ).to_wire())
            await websocket.close(code=AUTH_FAILURE_CLOSE_CODE, reason="rejected")
            self._connected.set()
            return
        await websocket.send(Envelope.make_response(envelope.id, "auth", {"account_id": self._expected_account_id}).to_wire())
        self._connected.set()
        try:
            async for raw in websocket:
                self._handle_incoming(raw)
        except ConnectionClosed:
            pass

    def _handle_incoming(self, raw: str) -> None:
        envelope = Envelope.from_wire(raw)
        if envelope.kind in (EnvelopeKind.RESPONSE, EnvelopeKind.ERROR):
            future = self._pending.pop(envelope.id, None)
            if future is not None and not future.done():
                future.set_result(envelope)
        elif envelope.kind == EnvelopeKind.EVENT:
            self._events.put(envelope)

    def send_request(self, method: str, payload: dict, timeout: float = 5.0) -> Envelope:
        if not self._connected.wait(timeout=timeout):
            raise AssertionError("FakeCloudPeer: connector never connected/authenticated within timeout.")

        async def _send_and_wait() -> Envelope:
            envelope = Envelope.make_request(method, payload)
            future: asyncio.Future = self._loop.create_future()
            self._pending[envelope.id] = future
            await self._ws.send(envelope.to_wire())
            return await asyncio.wait_for(future, timeout=timeout)

        return asyncio.run_coroutine_threadsafe(_send_and_wait(), self._loop).result(timeout=timeout + 1.0)

    def read_pushed_event(self, timeout: float = 5.0) -> Envelope:
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError("FakeCloudPeer: no event pushed within timeout.") from None

    def force_disconnect(self) -> None:
        """Closes the current connection from the server side, simulating
        an ordinary drop (not an auth rejection) -- re-arms `_connected`
        so a subsequent reconnect can be waited on again."""
        self._connected.clear()
        if self._ws is not None:
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop).result(timeout=5.0)

    def wait_for_reconnect(self, timeout: float = 5.0) -> None:
        if not self._connected.wait(timeout=timeout):
            raise AssertionError("FakeCloudPeer: no reconnect within timeout.")

    def stop(self) -> None:
        if self._loop is None or self._stopped:
            return
        self._stopped = True

        async def _shutdown() -> None:
            if self._ws is not None:
                try:
                    await self._ws.close()
                except Exception:
                    pass
            if self._server is not None:
                self._server.close()
                await self._server.wait_closed()

        try:
            asyncio.run_coroutine_threadsafe(_shutdown(), self._loop).result(timeout=5.0)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5.0)
