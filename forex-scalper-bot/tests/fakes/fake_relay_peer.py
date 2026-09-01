"""
FakeRelayPeer -- a test double playing the *connector* role of the relay
protocol: a real `websockets` client dialing a real RelayServer over
localhost TCP, so it exercises LocalConnectorBroker/RelayConnection
through their actual serialize/send/recv code path (not a pure in-memory
mock). An in-process socketpair/queue double was considered and rejected
for this -- it would leave RelayServer itself unexercised and defeat the
point of catching real framing bugs, per the approved Phase 5b design.

Scripted per RequestMethod name via exactly one of:
- script_response(method, payload) -- reply normally
- script_delay(method, payload, delay_seconds) -- reply, but only after
  sleeping first (to trigger ConnectorTimeoutError on the caller side
  when delay_seconds exceeds the caller's own timeout)
- script_drop(method) -- close the socket the instant this method's
  request arrives, with no reply at all (to trigger ConnectorOfflineError)
- script_error(method, *, error_type, message) -- reply with an
  `error`-kind envelope (to trigger BrokerRejectedError)

Runs its own background thread + event loop, entirely independent of
RelayServer/RelayConnection's loop on the other side of the socket --
two real, separately-threaded WebSocket peers over real TCP, matching
how the eventual real connector and cloud backend will actually talk.
"""
from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Optional

import websockets.asyncio.client as ws_client
from websockets.exceptions import ConnectionClosed

from relay_protocol.envelope import Envelope, EnvelopeKind


@dataclass
class _ScriptedReply:
    kind: str  # "response" | "delay" | "drop" | "error"
    payload: Optional[dict] = None
    delay_seconds: float = 0.0
    error_type: str = ""
    message: str = ""


class FakeRelayPeer:
    def __init__(self) -> None:
        self._scripts: dict[str, _ScriptedReply] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._connected = threading.Event()
        self._ws = None
        self._stopped = False

    def script_response(self, method: str, payload: dict) -> None:
        self._scripts[method] = _ScriptedReply(kind="response", payload=payload)

    def script_delay(self, method: str, payload: dict, delay_seconds: float) -> None:
        self._scripts[method] = _ScriptedReply(kind="delay", payload=payload, delay_seconds=delay_seconds)

    def script_drop(self, method: str) -> None:
        self._scripts[method] = _ScriptedReply(kind="drop")

    def script_error(self, method: str, *, error_type: str, message: str) -> None:
        self._scripts[method] = _ScriptedReply(kind="error", error_type=error_type, message=message)

    def push_event(self, method: str, payload: dict) -> None:
        """Sends a one-way `event` frame unprompted -- simulating the
        connector pushing a live quote/heartbeat/position-update, not
        replying to any request."""
        if self._loop is None or self._ws is None:
            raise RuntimeError("FakeRelayPeer is not connected yet.")

        async def _send() -> None:
            try:
                await self._ws.send(Envelope.make_event(method, payload).to_wire())
            except ConnectionClosed:
                pass

        asyncio.run_coroutine_threadsafe(_send(), self._loop).result(timeout=5.0)

    def start(self, url: str) -> None:
        # Runs its loop via run_forever (matching RelayServer's own
        # pattern), NOT run_until_complete(self._serve(url)) -- tying the
        # loop's lifetime to _serve() finishing created a real race in an
        # earlier version of this double: closing the websocket from a
        # separately-scheduled stop() coroutine could make _serve() (and
        # so run_until_complete, and so the whole thread) finish before
        # that stop() coroutine's own result was reported back through
        # run_coroutine_threadsafe, hanging stop() until its outer
        # timeout every single time. Keeping the loop alive independently
        # of _serve() removes that race entirely.
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self._connect_and_serve(url), self._loop)
        if not self._connected.wait(timeout=5.0):
            raise RuntimeError("FakeRelayPeer failed to connect within 5s.")

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _connect_and_serve(self, url: str) -> None:
        self._ws = await ws_client.connect(url)
        self._connected.set()
        try:
            async for raw in self._ws:
                if await self._handle_incoming(self._ws, raw):
                    break  # script_drop
        finally:
            # Always close our end when this loop ends -- whether from a
            # scripted drop or the server closing first -- so a drop
            # actually propagates as a closed connection (ConnectorOfflineError
            # on the caller side) rather than leaving a half-open socket
            # the caller would just time out waiting on instead.
            await self._ws.close()

    async def _handle_incoming(self, ws, raw: str) -> bool:
        envelope = Envelope.from_wire(raw)
        if envelope.kind != EnvelopeKind.REQUEST:
            return False
        script = self._scripts.get(envelope.method)
        if script is None:
            raise AssertionError(f"FakeRelayPeer has no script for method {envelope.method!r}.")
        if script.kind == "drop":
            return True
        try:
            if script.kind == "delay":
                await asyncio.sleep(script.delay_seconds)
                await ws.send(Envelope.make_response(envelope.id, envelope.method, script.payload).to_wire())
            elif script.kind == "error":
                await ws.send(Envelope.make_error(
                    envelope.id, envelope.method, error_type=script.error_type, message=script.message,
                ).to_wire())
            else:
                await ws.send(Envelope.make_response(envelope.id, envelope.method, script.payload).to_wire())
        except ConnectionClosed:
            # The requester already gave up (e.g. it hit its own timeout
            # and moved on, or the test tore the connection down) by the
            # time this reply -- especially a deliberately delayed one --
            # was ready to send. Nothing left to deliver it to.
            pass
        return False

    def stop(self) -> None:
        # Idempotent: a test may explicitly call stop() itself (e.g. to
        # simulate the connector disconnecting) and the owning fixture's
        # teardown always calls it again -- the second call must be a
        # cheap no-op, not a 5s hang scheduling work onto a loop that
        # already stopped running (run_coroutine_threadsafe onto a
        # non-running loop schedules but never executes the callback).
        if self._loop is None or self._stopped:
            return
        self._stopped = True

        async def _close() -> None:
            if self._ws is not None:
                await self._ws.close()

        try:
            asyncio.run_coroutine_threadsafe(_close(), self._loop).result(timeout=5.0)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5.0)
