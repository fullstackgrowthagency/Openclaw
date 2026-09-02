"""
Routes every blocking mt5.* call through one shared, single-worker
executor -- both relay_client.py's request handlers and main.py's
quote-polling/heartbeat loops use this, never calling MT5Client methods
directly from an async context. Same run_in_executor idiom already used
cloud-side for the pairing sqlite lookup during RelayConnection's auth
handshake (see fx_bot/brokers/local_connector/relay_connection.py in the
main forex-scalper-bot project).

`max_workers=1` is deliberate, not a default left unconsidered: whether
the MetaTrader5 package's IPC channel to the terminal is safe under
genuinely concurrent calls from multiple threads is unverified from the
confirmed API surface (see docs/ARCHITECTURE.md's Phase 5d section in
the main project). Serializing every call onto one dedicated thread
removes that risk regardless of what the underlying C extension actually
does, at a parallelism cost that's acceptable given MT5 IPC latency is
small and this process isn't otherwise CPU-bound.
"""
from __future__ import annotations

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

_T = TypeVar("_T")


class MT5Executor:
    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mt5-call")

    async def run(self, fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, functools.partial(fn, *args, **kwargs))

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True)
