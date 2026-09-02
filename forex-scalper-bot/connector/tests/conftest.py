"""
Shared test harness for running RelayClient.run_forever() concurrently
with a synchronous pytest test body -- its own background thread + event
loop (never sharing a loop with the test's fakes), matching the
run_forever-loop-lifecycle idiom used throughout this project's fakes.
"""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future
from dataclasses import dataclass

import pytest


@dataclass
class ClientRun:
    loop: asyncio.AbstractEventLoop
    thread: threading.Thread
    future: Future


def start_client_run_forever(client) -> ClientRun:
    loop = asyncio.new_event_loop()

    def _run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    thread = threading.Thread(target=_run_loop, daemon=True)
    thread.start()
    future = asyncio.run_coroutine_threadsafe(client.run_forever(), loop)
    return ClientRun(loop=loop, thread=thread, future=future)


def stop_client_run(run: ClientRun) -> None:
    run.future.cancel()
    try:
        # Give the cancelled task a real chance to unwind (its own
        # finally: await self._safe_close() needs the loop still running
        # to execute) before stopping the loop -- stopping immediately
        # after cancel() leaves it destroyed mid-suspension instead of
        # finished, the same class of teardown race already hit and
        # fixed twice on the cloud side of this project (FakeRelayPeer).
        run.future.result(timeout=3.0)
    except Exception:
        pass  # expected: CancelledError, or whatever run_forever last raised

    async def _drain_remaining_tasks() -> None:
        # websockets spawns its own supporting tasks per connection
        # (keepalive, transfer_data, ...) -- give any still-pending ones
        # (e.g. from a connection torn down mid-close) a beat to finish
        # too, rather than stopping the loop out from under them.
        current = asyncio.current_task()
        pending = [t for t in asyncio.all_tasks(run.loop) if t is not current and not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=1.0)

    try:
        asyncio.run_coroutine_threadsafe(_drain_remaining_tasks(), run.loop).result(timeout=2.0)
    except Exception:
        pass
    run.loop.call_soon_threadsafe(run.loop.stop)
    run.thread.join(timeout=5.0)


@pytest.fixture
def client_run():
    """Yields start_client_run_forever; tears down whatever run(s) the
    test started, tracked via a list the test appends to."""
    runs: list[ClientRun] = []

    def _start(client) -> ClientRun:
        run = start_client_run_forever(client)
        runs.append(run)
        return run

    try:
        yield _start
    finally:
        for run in runs:
            stop_client_run(run)
