"""
Reference wiring for the live/sandbox pipeline. This is intentionally
minimal -- it shows how the pieces connect (broker -> scanner -> watcher ->
trigger engine -> risk engine -> order manager -> position manager) without
prescribing a production run-loop, since streaming subscription details
depend on the real Webull SDK integration (Phase 2, not yet implemented --
see brokers/webull/client.py).

Run with `python -m webull_bot.main` once WebullBrokerClient is wired up.
Until then this will raise NotImplementedError by design when it tries to
connect/stream, since PAPER mode is the only backend that works out of the
box (see brokers/paper/client.py) and has no live market data of its own.
"""
from __future__ import annotations

from webull_bot.brokers import get_broker_client
from webull_bot.config import get_settings
from webull_bot.data.float_providers.cache import CachedFloatProvider
from webull_bot.data.float_providers.massive import MassiveFloatProvider
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

    float_provider = CachedFloatProvider(
        MassiveFloatProvider(settings.massive), settings.float_cache_dir, settings.float_cache_ttl_hours
    )

    broad_scanner = BroadScanner(broker, float_provider)
    watcher = CandidateWatcher()
    trigger_engine = TriggerEngine(strategies=[MomentumBreakoutStrategy()])
    risk_engine = RiskEngine()
    order_manager = OrderManager(broker, risk_engine, settings)

    print(
        "Wiring is constructed. Streaming run-loop (broker.subscribe_quotes -> "
        "watcher.update -> trigger_engine.on_snapshot -> order_manager.submit_signal) "
        "is Phase 2/3 work, pending the real Webull streaming integration."
    )
    # Intentionally not started here -- see module docstring.
    _ = (broad_scanner, watcher, trigger_engine, order_manager)


if __name__ == "__main__":
    main()
