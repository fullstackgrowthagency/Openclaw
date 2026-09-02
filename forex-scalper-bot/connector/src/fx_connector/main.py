"""
Entrypoint wiring everything together: obtain/load pairing credentials,
connect to MT5, connect to the relay with reconnect-with-backoff, and run
the quote-polling/heartbeat loops alongside it.

`_import_real_mt5()` is the ONLY place `import MetaTrader5` ever
executes anywhere in this project, and it happens inside a function, not
at module load time -- every other module takes an injected `mt5_module`
object instead, which is what keeps this whole package importable and
testable on Linux (see this project's README).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .config import ConnectorSettings, get_settings
from .mt5_client import MT5Client, MT5ClientError
from .mt5_executor import MT5Executor
from .pairing import load_credentials, prompt_and_pair
from .relay_client import AuthFailure, RelayClient

logger = logging.getLogger(__name__)


def _import_real_mt5() -> Any:
    import MetaTrader5 as mt5  # noqa: N813 -- matches the package's own documented import convention

    return mt5


async def _run_relay_with_repairing(relay: RelayClient, settings: ConnectorSettings) -> None:
    while True:
        try:
            await relay.run_forever()
        except AuthFailure:
            logger.error("Pairing token was rejected -- re-pairing is required.")
            settings.token_file_path.unlink(missing_ok=True)
            new_creds = prompt_and_pair(settings.pairing_base_url, settings.token_file_path)
            relay.update_credentials(new_creds.token, new_creds.account_id)


async def _run_quote_loop(relay: RelayClient, mt5_client: MT5Client, mt5_executor: MT5Executor, settings: ConnectorSettings) -> None:
    last_snapshot: dict[str, Any] = {}
    mt5_healthy = True
    while True:
        for symbol in list(relay.subscribed_symbols):
            try:
                snapshot = await mt5_executor.run(mt5_client.get_snapshot, symbol)
            except MT5ClientError:
                continue
            prev = last_snapshot.get(symbol)
            if prev is None or snapshot.bid != prev.bid or snapshot.ask != prev.ask:
                await relay.push_quote(snapshot)
                last_snapshot[symbol] = snapshot

        healthy_now = await mt5_executor.run(mt5_client.is_connected)
        if healthy_now and not mt5_healthy:
            await relay.push_mt5_reconnected()
        elif not healthy_now and mt5_healthy:
            await relay.push_mt5_disconnected("account_info() returned None")
        mt5_healthy = healthy_now

        await asyncio.sleep(settings.quote_poll_interval_seconds)


async def _run_heartbeat_loop(relay: RelayClient, settings: ConnectorSettings) -> None:
    while True:
        await relay.push_heartbeat()
        await asyncio.sleep(settings.heartbeat_interval_seconds)


async def async_main() -> None:
    settings = get_settings()

    creds = load_credentials(settings.token_file_path)
    if creds is None:
        creds = prompt_and_pair(settings.pairing_base_url, settings.token_file_path)

    mt5_client = MT5Client(
        _import_real_mt5(), login=settings.mt5_login, password=settings.mt5_password,
        server=settings.mt5_server, symbol_suffix=settings.symbol_suffix, path=settings.mt5_path,
    )
    mt5_client.connect()
    mt5_executor = MT5Executor()

    relay = RelayClient(
        settings.relay_ws_url, token=creds.token, account_id=creds.account_id,
        mt5_client=mt5_client, mt5_executor=mt5_executor,
        request_timeout=settings.request_timeout_seconds,
        backoff_base=settings.reconnect_base_seconds, backoff_cap=settings.reconnect_cap_seconds,
    )

    await asyncio.gather(
        _run_relay_with_repairing(relay, settings),
        _run_quote_loop(relay, mt5_client, mt5_executor, settings),
        _run_heartbeat_loop(relay, settings),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
