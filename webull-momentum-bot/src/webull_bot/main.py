"""
Reference wiring for the live/sandbox pipeline. This is intentionally
minimal -- it shows how the pieces connect (broker -> scanner -> watcher ->
trigger engine -> risk engine -> order manager -> position manager) without
prescribing a production run-loop.

Run with `python -m webull_bot.main`. Account/market-data calls work in both
PAPER mode (fully local, see brokers/paper/client.py) and SANDBOX mode
(real Webull sandbox account, verified live -- see brokers/webull/client.py).
Streaming (broker.subscribe_quotes) is NOT implemented for Webull yet: its
sandbox MQTT host was never confirmed, so a real run-loop that reacts to
live ticks still needs that piece before this can run unattended. Poll
get_snapshot()/get_bars() in the meantime.
"""
from __future__ import annotations

from webull_bot.brokers import get_broker_client
from webull_bot.config import get_settings
from webull_bot.data.float_providers import get_float_provider
from webull_bot.risk.risk_engine import RiskEngine
from webull_bot.scanner.broad_scanner import BroadScanner
from webull_bot.scanner.candidate_watcher import CandidateWatcher
from webull_bot.scanner.trigger_engine import TriggerEngine
from webull_bot.strategy.momentum_breakout import MomentumBreakoutStrategy
from webull_bot.execution.order_manager import OrderManager


def main() -> None:
    settings = get_settings()
    settings.require_non_live_or_authorized()

    print(f"Starting in trading_mode={settings.trading_mode.value} (environment={settings.environment.value})")

    broker = get_broker_client(settings)
    broker.connect()

    float_provider = get_float_provider(settings)

    broad_scanner = BroadScanner(broker, float_provider)
    watcher = CandidateWatcher()
    trigger_engine = TriggerEngine(strategies=[MomentumBreakoutStrategy()])
    risk_engine = RiskEngine()
    order_manager = OrderManager(broker, risk_engine, settings)

    print(
        "Wiring is constructed. Streaming run-loop (broker.subscribe_quotes -> "
        "watcher.update -> trigger_engine.on_snapshot -> order_manager.submit_signal) "
        "still needs a confirmed Webull sandbox MQTT host before it can react "
        "to live ticks unattended; poll-based wiring can be built now."
    )
    # Intentionally not started here -- see module docstring.
    _ = (broad_scanner, watcher, trigger_engine, order_manager)


if __name__ == "__main__":
    main()
