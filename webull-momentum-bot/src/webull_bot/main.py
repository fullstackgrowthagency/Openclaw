"""
Entrypoint: builds the full pipeline and runs the poll-based TradingLoop.

Run with `python -m webull_bot.main`. Account/market-data calls work in both
PAPER mode (fully local, see brokers/paper/client.py) and SANDBOX mode
(real Webull sandbox account, verified live -- see brokers/webull/client.py).

Streaming (broker.subscribe_quotes) is NOT implemented for Webull yet: its
sandbox MQTT host was never confirmed. TradingLoop (runtime/trading_loop.py)
polls broker.get_snapshot() per candidate instead of reacting to a push
feed -- see that module's docstring for how it also copes with
WebullBrokerClient.place_order returning SUBMITTED rather than FILLED.

PAPER mode has no real market universe of its own (PaperBrokerClient is a
pure execution simulator with no live quotes) -- it falls back to a small
static watchlist here, meant for exercising the pipeline against snapshots
you feed it yourself (see brokers/paper/client.py's feed_snapshot), not for
autonomous discovery.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from webull_bot.brokers import get_broker_client
from webull_bot.brokers.webull.client import WebullBrokerClient
from webull_bot.collection.event_recorder import MomentumEventTracker
from webull_bot.config import Settings, get_settings
from webull_bot.data.float_providers import get_float_provider
from webull_bot.data.universe import (
    MultiSourceUniverseProvider,
    StaticUniverseProvider,
    WebullGainersLosersConfig,
    WebullGainersLosersUniverseProvider,
    WebullUniverseConfig,
    WebullUniverseProvider,
)
from webull_bot.enums import CandidateState
from webull_bot.execution.order_manager import OrderManager
from webull_bot.models import MomentumScore, Order, Trade
from webull_bot.position.position_manager import PositionManager
from webull_bot.risk.risk_engine import RiskEngine
from webull_bot.runtime.trading_loop import TradingLoop
from webull_bot.scanner.broad_scanner import BroadScanner
from webull_bot.scanner.candidate_watcher import CandidateWatcher
from webull_bot.scanner.trigger_engine import TriggerEngine
from webull_bot.strategy.momentum_breakout import MomentumBreakoutStrategy

_PAPER_MODE_PLACEHOLDER_WATCHLIST = ["AAPL"]  # replace with symbols you intend to feed snapshots for


def build_trading_loop(
    *,
    settings: Optional[Settings] = None,
    on_trade_closed: Optional[Callable[[Trade], None]] = None,
    on_order_update: Optional[Callable[[Order], None]] = None,
    on_state_transition: Optional[Callable[[str, CandidateState, CandidateState, datetime], None]] = None,
    on_score_computed: Optional[Callable[[str, MomentumScore], None]] = None,
    momentum_event_tracker: Optional[MomentumEventTracker] = None,
) -> TradingLoop:
    """Builds the full pipeline. `on_trade_closed`/`on_order_update` default to
    printing (this module's original behavior); pass your own (and the other
    hooks) to also (or instead) persist to a database -- see
    scripts/run_dashboard.py, which wraps these to write through
    db/repository.py while still printing trade closures."""
    settings = settings or get_settings()
    settings.require_non_live_or_authorized()

    print(f"Starting in trading_mode={settings.trading_mode.value} (environment={settings.environment.value})")

    broker = get_broker_client(settings)
    broker.connect()

    float_provider = get_float_provider(settings)
    broad_scanner = BroadScanner(broker, float_provider)

    if isinstance(broker, WebullBrokerClient):
        # Three independent discovery sources, unioned (not a priority
        # fallback chain) -- each catches a different kind of momentum:
        # relative volume, float turnover, and raw price change. A symbol
        # only needs to show up on ONE list; BroadScanner vets every symbol
        # the same way regardless of which list(s) surfaced it.
        universe_provider = MultiSourceUniverseProvider([
            WebullUniverseProvider.from_broker(broker, WebullUniverseConfig(rank_type="RELATIVE_VOLUME_10D")),
            WebullUniverseProvider.from_broker(broker, WebullUniverseConfig(rank_type="TURNOVER_RATE")),
            WebullGainersLosersUniverseProvider.from_broker(
                broker, WebullGainersLosersConfig(rank_type="DAY_1", sort_by="CHANGE_RATIO", direction="DESC")
            ),
        ])
    else:
        universe_provider = StaticUniverseProvider(_PAPER_MODE_PLACEHOLDER_WATCHLIST)

    watcher = CandidateWatcher()
    trigger_engine = TriggerEngine(strategies=[MomentumBreakoutStrategy()])
    risk_engine = RiskEngine()
    order_manager = OrderManager(broker, risk_engine, settings)
    position_manager = PositionManager()

    return TradingLoop(
        broker, universe_provider, broad_scanner, watcher, trigger_engine,
        order_manager, position_manager, risk_engine,
        on_trade_closed=on_trade_closed or (lambda trade: print(f"TRADE CLOSED: {trade}")),
        on_order_update=on_order_update,
        on_state_transition=on_state_transition,
        on_score_computed=on_score_computed,
        momentum_event_tracker=momentum_event_tracker,
    )


def main() -> None:
    loop = build_trading_loop()
    print("Trading loop constructed. Starting poll loop (Ctrl+C to stop)...")
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
