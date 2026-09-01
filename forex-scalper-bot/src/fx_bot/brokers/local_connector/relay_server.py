"""
RelayServer -- the cloud-side WebSocket listener: accepts inbound
connector sockets and wraps each in a RelayConnection sharing this
server's one background event loop. No per-account dispatch/routing
layer in front of `accept()` yet -- production wiring of a real
entrypoint (running this alongside the pairing HTTP app against the same
PairingStore) is out of scope for Phase 5c, deferred to whenever the
connector itself needs something real to dial (5d+); see the approved
Phase 5c design's own note on this.

Phase 5c: every accepted socket must authenticate (see
RelayConnection._authenticate) BEFORE it is ever queued for `accept()`
to pick up -- `accept()` therefore only ever returns an authenticated
connection, never a bare one waiting to be authed later.
"""
from __future__ import annotations

import asyncio
import queue
import threading
from typing import Callable, Optional

import websockets.asyncio.server as ws_server

from .relay_connection import RelayConnection


class RelayServer:
    def __init__(
        self, host: str = "127.0.0.1", port: int = 0, *,
        authenticator: Callable[[str], Optional[str]],
        default_timeout: float = 5.0,
        auth_grace_seconds: float = 10.0,
    ):
        self._host = host
        self._requested_port = port
        self._default_timeout = default_timeout
        self._authenticator = authenticator
        self._auth_grace_seconds = auth_grace_seconds
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: ws_server.Server | None = None
        self._port: int | None = None
        self._connections: "queue.Queue[RelayConnection]" = queue.Queue()
        self._stopped = False

    def start(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self._start_serving(), self._loop).result(timeout=5.0)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _start_serving(self) -> None:
        self._server = await ws_server.serve(self._handle_connection, self._host, self._requested_port)
        self._port = self._server.sockets[0].getsockname()[1]

    async def _handle_connection(self, websocket) -> None:
        conn = RelayConnection(self._loop, websocket, default_timeout=self._default_timeout)
        authenticated = await conn._authenticate(self._authenticator, grace_seconds=self._auth_grace_seconds)
        if not authenticated:
            return  # _authenticate already closed the socket; nothing queued
        self._connections.put_nowait(conn)
        # `websockets` closes the socket the instant this handler
        # returns, so the handler's lifetime IS the connection's lifetime
        # -- this blocks here for as long as the connector stays connected.
        await conn._read_loop()

    def accept(self, timeout: float = 5.0) -> RelayConnection:
        """Blocks for the next inbound connector connection."""
        return self._connections.get(timeout=timeout)

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        if self._port is None:
            raise RuntimeError("RelayServer has not been started yet.")
        return self._port

    def stop(self) -> None:
        # Idempotent for the same reason FakeRelayPeer.stop() is -- a
        # second call must not schedule work onto a loop that already
        # stopped running (see that class's stop() docstring/comment).
        if self._loop is None or self._stopped:
            return
        self._stopped = True

        async def _shutdown() -> None:
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
