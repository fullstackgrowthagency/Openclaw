"""
Backtest engine -- reuses the EXACT same Strategy/RiskEngine/OrderManager
code live trading will use, only ever swapping in PaperBrokerClient,
mirroring webull-momentum-bot/src/webull_bot/backtest/engine.py's own
"a strategy that only works in a bespoke backtest-only code path proves
nothing about how it will behave live" principle. This is the Phase 2
skeleton version: feeds a pre-sorted, already-chronological list of bars
through the pipeline one at a time. No cross-symbol interleaving/no-
lookahead guarantees are implemented yet (the equities bot's own version
only needed that once it handled multiple concurrently-tracked symbols;
add it here once this bot does too).
"""
from __future__ import annotations

from ..execution.order_manager import OrderManager
from ..interfaces.strategy import Strategy
from ..models import MarketSnapshot, Trade
from ..brokers.paper.client import PaperBrokerClient


class BacktestEngine:
    def __init__(
        self, strategy: Strategy, broker: PaperBrokerClient, order_manager: OrderManager, history_lookback: int = 500,
    ):
        self.strategy = strategy
        self.broker = broker
        self.order_manager = order_manager
        self.history_lookback = history_lookback

    def run(self, bars: list[MarketSnapshot]) -> list[Trade]:
        for snapshot in sorted(bars, key=lambda bar: bar.timestamp):
            self.broker.feed_snapshot(snapshot)
            history = self.broker.get_bars(snapshot.symbol, "1m", lookback=self.history_lookback)
            open_positions = self.broker.get_positions()
            signal = self.strategy.on_snapshot(snapshot.symbol, snapshot, history)
            if signal is not None:
                self.order_manager.submit_signal(signal, snapshot=snapshot, open_positions=open_positions)
        return self.broker.trades
