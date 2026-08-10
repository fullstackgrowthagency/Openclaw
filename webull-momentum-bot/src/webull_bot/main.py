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
from webull_bot.strategy.breakout_pullback import BreakoutPullbackStrategy
from webull_bot.strategy.ignition_pullback import IgnitionPullbackStrategy
from webull_bot.strategy.momentum_breakout import MomentumBreakoutStrategy
from webull_bot.strategy.opening_range_breakout import OpeningRangeBreakoutStrategy
from webull_bot.strategy.refined_breakout import RefinedBreakoutStrategy
from webull_bot.strategy.volatility_contraction import VolatilityContractionBreakoutStrategy
from webull_bot.strategy.volume_ignition import VolumeIgnitionStrategy
from webull_bot.strategy.vwap_reclaim import VWAPReclaimStrategy

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
        # Four independent discovery sources, unioned (not a priority
        # fallback chain) -- each catches a different kind of momentum:
        # relative volume, float turnover, today's raw price change, and
        # the last 5 minutes' raw price change. A symbol only needs to
        # show up on ONE list; BroadScanner vets every symbol the same way
        # regardless of which list(s) surfaced it.
        #
        # All four sources set rank_value_field/min_rank_value -- pagination
        # WITHOUT one was confirmed live (2026-08-09) to be a real problem,
        # not a theoretical one: each of TURNOVER_RATE/DAY_1/MIN_5 paginated
        # all the way to max_pages (2000 raw results) and still hadn't
        # dropped below any meaningful activity level, landing 950-1100+
        # symbols each (RELATIVE_VOLUME_10D, the one source with a threshold
        # from the start, landed at a much more reasonable 301). Across 4
        # sources that's thousands of symbols/cycle -- an actual API-call
        # explosion at BroadScanner's ~1.25-2.86s/symbol, not the rare
        # circuit-breaker case max_pages was designed for. With the
        # thresholds below, the same live check landed at 301/146/195/88
        # per source and 547 unique symbols combined after dedup -- a real,
        # substantial increase in coverage over the old fixed-100-per-source
        # cap (previously ~150 combined), at a real, substantial increase in
        # per-cycle scan time (see docs/ARCHITECTURE.md). These three
        # thresholds are first-pass estimates from the live decay curves
        # observed that day, not backtested -- tune them with real data
        # before trusting the exact cutoff.
        universe_provider = MultiSourceUniverseProvider([
            WebullUniverseProvider.from_broker(broker, WebullUniverseConfig(
                # Reuses scoring/weights.yaml's min_relative_volume_for_watch.
                rank_type="RELATIVE_VOLUME_10D", rank_value_field="relative_volume_10d", min_rank_value=2.0,
            )),
            WebullUniverseProvider.from_broker(broker, WebullUniverseConfig(
                # 0.10 = 10% of float traded that day -- still notably
                # active, roughly where the live decay curve was by page 3-4.
                rank_type="TURNOVER_RATE", rank_value_field="turnover_rate", min_rank_value=0.10,
            )),
            WebullGainersLosersUniverseProvider.from_broker(broker, WebullGainersLosersConfig(
                # 0.10 = 10% gain/loss for the full day -- a genuinely
                # notable full-day move for this bot's target universe.
                rank_type="DAY_1", sort_by="CHANGE_RATIO", direction="DESC",
                rank_value_field="change_ratio", min_rank_value=0.10,
            )),
            # 4th source: Webull's real, verified 5-minute price-change
            # ranking -- see WebullGainersLosersUniverseProvider's
            # docstring for the live confirmation and why there's no
            # 5-minute *volume* equivalent to pair with it. Threshold is
            # lower than DAY_1's (0.05 vs 0.10) since the same % move
            # packed into 5 minutes instead of a full day is a much more
            # intense signal -- requiring DAY_1's bar here would miss real
            # ignition moves.
            WebullGainersLosersUniverseProvider.from_broker(broker, WebullGainersLosersConfig(
                rank_type="MIN_5", sort_by="CHANGE_RATIO", direction="DESC",
                rank_value_field="change_ratio", min_rank_value=0.05,
            )),
        ])
    else:
        universe_provider = StaticUniverseProvider(_PAPER_MODE_PLACEHOLDER_WATCHLIST)

    watcher = CandidateWatcher()
    # Constructed before the strategies below so each one can be handed a
    # live-reading closure over its config -- see reward_risk_ratio_fn.
    risk_engine = RiskEngine()

    # Every strategy computes its target as entry + risk_per_share * this
    # ratio, read fresh on every signal rather than each hardcoding its own
    # fixed multiple -- so tightening/loosening RiskConfig.min_risk_reward_ratio
    # from the dashboard's Settings panel (see risk/risk_engine.py) changes
    # what every strategy actually targets, not just what the risk engine's
    # entry gate demands. Same object the gate itself reads, so a strategy's
    # own signal can never fail that gate on account of the target it just computed.
    reward_risk_ratio_fn = lambda: risk_engine.config.min_risk_reward_ratio  # noqa: E731

    # Ordered most-selective/confirmed first, most-permissive last -- the
    # first strategy to return a Signal for a given tick wins (see
    # TriggerEngine), so a broad catch-all placed early would pre-empt
    # more specific patterns from ever getting a chance to fire. See
    # docs/ARCHITECTURE.md's "Entry strategies" section for what each one
    # catches and why it's ordered here the way it is.
    trigger_engine = TriggerEngine(strategies=[
        RefinedBreakoutStrategy(reward_risk_ratio_fn=reward_risk_ratio_fn),
        OpeningRangeBreakoutStrategy(reward_risk_ratio_fn=reward_risk_ratio_fn),
        VWAPReclaimStrategy(reward_risk_ratio_fn=reward_risk_ratio_fn),
        MomentumBreakoutStrategy(reward_risk_ratio_fn=reward_risk_ratio_fn),
        BreakoutPullbackStrategy(reward_risk_ratio_fn=reward_risk_ratio_fn),
        IgnitionPullbackStrategy(reward_risk_ratio_fn=reward_risk_ratio_fn),
        VolatilityContractionBreakoutStrategy(reward_risk_ratio_fn=reward_risk_ratio_fn),
        VolumeIgnitionStrategy(reward_risk_ratio_fn=reward_risk_ratio_fn),
    ])
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
