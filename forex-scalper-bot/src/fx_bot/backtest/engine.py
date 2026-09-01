"""
Backtest engine -- reuses the EXACT same Strategy/RiskEngine/OrderManager/
PositionManager code live trading will use, only ever swapping in
PaperBrokerClient, mirroring webull-momentum-bot/src/webull_bot/backtest/
engine.py's own "a strategy that only works in a bespoke backtest-only
code path proves nothing about how it will behave live" principle. No
cross-symbol interleaving/no-lookahead guarantees are implemented yet
(the equities bot's own version only needed that once it handled multiple
concurrently-tracked symbols; add it here once this bot does too).
"""
from __future__ import annotations

from ..enums import SignalAction
from ..execution.order_manager import OrderManager
from ..interfaces.strategy import Strategy
from ..models import MarketSnapshot, Signal, Trade
from ..brokers.paper.client import PaperBrokerClient
from ..position.position_manager import PositionManager


class BacktestEngine:
    def __init__(
        self,
        strategy: Strategy,
        broker: PaperBrokerClient,
        order_manager: OrderManager,
        position_manager: PositionManager | None = None,
        history_lookback: int = 500,
    ):
        self.strategy = strategy
        self.broker = broker
        self.order_manager = order_manager
        self.position_manager = position_manager or PositionManager()
        self.history_lookback = history_lookback

    def run(self, bars: list[MarketSnapshot]) -> list[Trade]:
        for snapshot in sorted(bars, key=lambda bar: bar.timestamp):
            self.broker.feed_snapshot(snapshot)
            open_positions = self.broker.get_positions()
            position = next((p for p in open_positions if p.symbol == snapshot.symbol), None)

            # Protective checks run BEFORE the strategy gets a say --
            # stop-loss/target/trailing/breakeven can close a position the
            # strategy itself has no opinion on this tick (see
            # position/position_manager.py's docstring for why nothing
            # auto-enforced these before this phase).
            if position is not None:
                exit_reason = self.position_manager.manage(position, snapshot)
                if exit_reason is not None:
                    auto_exit_signal = Signal(
                        symbol=snapshot.symbol, action=SignalAction.EXIT, generated_at=snapshot.timestamp,
                        strategy_name=position.strategy_name, strategy_version="position_manager",
                        reference_price=snapshot.mid, metadata={"exit_reason": exit_reason.value},
                    )
                    self.order_manager.submit_signal(auto_exit_signal, snapshot=snapshot, open_positions=open_positions)
                    # Refresh -- the position manager's own close above may
                    # have just removed it, and the strategy below must see
                    # the POST-close reality, not the stale pre-close state.
                    open_positions = self.broker.get_positions()
                    position = next((p for p in open_positions if p.symbol == snapshot.symbol), None)

            history = self.broker.get_bars(snapshot.symbol, "1m", lookback=self.history_lookback)
            signal = self.strategy.on_snapshot(snapshot.symbol, snapshot, history, position)
            if signal is not None:
                self.order_manager.submit_signal(signal, snapshot=snapshot, open_positions=open_positions)
        return self.broker.trades
