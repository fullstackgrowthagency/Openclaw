"""
main.py wiring tests -- deliberately exercise the standalone loop
functions (_run_quote_loop) and the pairing-trigger decision directly
rather than running the full async_main() end to end for its own sake:
that function's asyncio.gather(...) runs three infinite loops and its
very first real step imports the actual (Windows-only) MetaTrader5
package via _import_real_mt5(), which this environment can't and
shouldn't exercise. What's tested here is exactly the logic Phase 5d
added on top of RelayClient/MT5Client, both already covered by their own
dedicated test files -- _import_real_mt5/RelayClient.run_forever are
monkeypatched away specifically so async_main() itself can still be
driven far enough to prove the pairing-trigger wiring works.
"""
from __future__ import annotations

import asyncio
import contextlib

from fx_connector import main as main_module
from fx_connector.config import ConnectorSettings
from fx_connector.mt5_client import MT5Client
from fx_connector.mt5_executor import MT5Executor
from fx_connector.pairing import PairingCredentials
from tests.fakes.fake_mt5_module import FakeMT5Module


class _FakeRelay:
    def __init__(self):
        self.subscribed_symbols: frozenset = frozenset()
        self.pushed_quotes = []
        self.pushed_events = []

    async def push_quote(self, snapshot) -> None:
        self.pushed_quotes.append(snapshot)

    async def push_heartbeat(self, payload=None) -> None:
        self.pushed_events.append(("heartbeat", payload))

    async def push_mt5_disconnected(self, reason) -> None:
        self.pushed_events.append(("mt5_disconnected", reason))

    async def push_mt5_reconnected(self) -> None:
        self.pushed_events.append(("mt5_reconnected", None))


async def _run_and_cancel(coro, *, warm_up: float) -> None:
    task = asyncio.create_task(coro)
    await asyncio.sleep(warm_up)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def test_quote_loop_pushes_event_only_on_price_change():
    mt5 = FakeMT5Module()
    mt5.set_tick("EURUSD", bid=1.1000, ask=1.1002)
    client = MT5Client(mt5)
    executor = MT5Executor()
    relay = _FakeRelay()
    relay.subscribed_symbols = frozenset({"EUR/USD"})
    settings = ConnectorSettings(quote_poll_interval_seconds=0.02)

    async def _scenario() -> None:
        task = asyncio.create_task(main_module._run_quote_loop(relay, client, executor, settings))
        await asyncio.sleep(0.06)  # several unchanged-price iterations
        mt5.set_tick("EURUSD", bid=1.1010, ask=1.1012)
        await asyncio.sleep(0.06)  # several changed-price iterations
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(_scenario())

    # One push for the initial tick, one for the change -- no duplicates
    # despite many unchanged iterations either side of the change.
    assert len(relay.pushed_quotes) == 2


def test_quote_loop_pushes_mt5_disconnected_then_reconnected_on_edge_transition():
    mt5 = FakeMT5Module()
    client = MT5Client(mt5)
    executor = MT5Executor()
    relay = _FakeRelay()
    settings = ConnectorSettings(quote_poll_interval_seconds=0.02)
    original_account_info = mt5._account_info

    async def _scenario() -> None:
        task = asyncio.create_task(main_module._run_quote_loop(relay, client, executor, settings))
        await asyncio.sleep(0.03)  # healthy
        mt5._account_info = None  # simulate MT5 disconnecting
        await asyncio.sleep(0.05)
        mt5._account_info = original_account_info  # reconnect
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(_scenario())

    event_names = [name for name, _ in relay.pushed_events]
    assert "mt5_disconnected" in event_names
    assert "mt5_reconnected" in event_names
    assert event_names.index("mt5_disconnected") < event_names.index("mt5_reconnected")


def test_async_main_triggers_pairing_when_no_token_saved(monkeypatch, tmp_path):
    calls = {"prompt_and_pair": 0}

    monkeypatch.setattr(main_module, "load_credentials", lambda path: None)

    def fake_prompt_and_pair(base_url, path, *, code=None):
        calls["prompt_and_pair"] += 1
        return PairingCredentials(token="tok", account_id="acct")

    monkeypatch.setattr(main_module, "prompt_and_pair", fake_prompt_and_pair)
    monkeypatch.setattr(main_module, "_import_real_mt5", lambda: FakeMT5Module())

    async def _never_returns(self) -> None:
        await asyncio.sleep(1000)

    monkeypatch.setattr(main_module.RelayClient, "run_forever", _never_returns)

    settings = ConnectorSettings(
        token_file_path=tmp_path / "creds.json", quote_poll_interval_seconds=0.01, heartbeat_interval_seconds=1000,
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    async def _run() -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(main_module.async_main(), timeout=0.1)

    asyncio.run(_run())

    assert calls["prompt_and_pair"] == 1
