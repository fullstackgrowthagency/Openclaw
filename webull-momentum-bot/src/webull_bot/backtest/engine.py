"""
Event-driven backtest engine.

Uses the exact same Strategy / RiskEngine / OrderManager / CandidateWatcher /
TriggerEngine / PositionManager code paths as live trading -- only the
broker is swapped for PaperBrokerClient, which applies a configurable
slippage/fee model on every fill. This is deliberate: a strategy that only
"works" in a bespoke backtest-only code path proves nothing about how it
will behave live.

No-lookahead guarantee: snapshots across all symbols are merged into a
single chronological timeline and processed one at a time, so a symbol's
processing at time T never sees another symbol's (or its own) bar from
T' > T.

NOT modeled yet (tracked as follow-up work, not silently ignored):
  - Trading halts
  - Order latency
  - Partial fills / queue position
  - Level 2 / order-flow-aware fills
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..brokers.paper.client import PaperBrokerClient, PaperBrokerConfig
from ..config import Settings, get_settings
from ..enums import CandidateState, ExitReason, OrderStatus
from ..execution.order_manager import OrderManager, OrderRejected
from ..interfaces.strategy import Strategy
from ..models import Candidate, FloatData, MarketSnapshot, Trade
from ..position.position_manager import PositionManagementConfig, PositionManager
from ..risk.risk_engine import RiskConfig, RiskEngine
from ..scanner.candidate_watcher import CandidateWatcher, WatcherConfig
from ..scanner.trigger_engine import TriggerEngine
from ..scoring.momentum_ignition_score import MISConfig
from ..state_machine import new_candidate, transition


@dataclass
class BacktestConfig:
    starting_equity: float = 25_000.0
    fill_slippage_bps: float = 5.0
    fee_per_share: float = 0.0


@dataclass
class BacktestResultSummary:
    trades: list[Trade] = field(default_factory=list)
    ending_equity: float = 0.0

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.pnl > 0)
        return wins / len(self.trades)


class BacktestEngine:
    def __init__(
        self,
        strategies: list[Strategy],
        *,
        risk_config: Optional[RiskConfig] = None,
        position_config: Optional[PositionManagementConfig] = None,
        mis_config: Optional[MISConfig] = None,
        watcher_config: Optional[WatcherConfig] = None,
        config: Optional[BacktestConfig] = None,
        settings: Optional[Settings] = None,
    ):
        self.config = config or BacktestConfig()
        self.broker = PaperBrokerClient(
            PaperBrokerConfig(
                starting_equity=self.config.starting_equity,
                fill_slippage_bps=self.config.fill_slippage_bps,
                fee_per_share=self.config.fee_per_share,
            )
        )
        self.broker.connect()
        self.risk_engine = RiskEngine(risk_config)
        self.order_manager = OrderManager(self.broker, self.risk_engine, settings or get_settings())
        self.watcher = CandidateWatcher(mis_config, watcher_config)
        self.trigger_engine = TriggerEngine(strategies)
        self.position_manager = PositionManager(position_config)
        self.candidates: dict[str, Candidate] = {}
        self.trades: list[Trade] = []

    def run(
        self, symbol_bars: dict[str, list[MarketSnapshot]], float_data: Optional[dict[str, FloatData]] = None
    ) -> BacktestResultSummary:
        float_data = float_data or {}
        timeline = sorted(
            (snapshot for bars in symbol_bars.values() for snapshot in bars),
            key=lambda s: s.timestamp,
        )
        for snapshot in timeline:
            self._process_snapshot(snapshot, float_data.get(snapshot.symbol))

        return BacktestResultSummary(trades=self.trades, ending_equity=self.broker.get_account_equity())

    def _get_or_seed_candidate(self, symbol: str, snapshot: MarketSnapshot, float_data: Optional[FloatData]) -> Candidate:
        candidate = self.candidates.get(symbol)
        if candidate is None:
            candidate = new_candidate(symbol, now=snapshot.timestamp)
            candidate.float_data = float_data
            transition(candidate, CandidateState.WATCHING, now=snapshot.timestamp, reason="backtest seed")
            self.candidates[symbol] = candidate
        return candidate

    def _process_snapshot(self, snapshot: MarketSnapshot, float_data: Optional[FloatData]) -> None:
        self.broker.feed_snapshot(snapshot)
        candidate = self._get_or_seed_candidate(snapshot.symbol, snapshot, float_data)

        open_position = next((p for p in self.broker.get_positions() if p.symbol == snapshot.symbol), None)
        if open_position is not None:
            exit_signal = self.position_manager.check_exit(open_position, snapshot)
            if exit_signal is not None:
                self._execute_exit(candidate, open_position, exit_signal, snapshot)
            return  # already in (or just exited) a position this bar; skip entry logic

        if candidate.state in (CandidateState.REJECTED,):
            return

        self.watcher.update(candidate, snapshot)
        signal = self.trigger_engine.on_snapshot(candidate, snapshot)
        # Roll this bar's high into resistance for the *next* bar only after
        # the trigger engine has checked it against the current level.
        self.watcher.update_resistance(candidate, snapshot)
        if signal is None:
            return

        try:
            order = self.order_manager.submit_signal(signal, snapshot=snapshot)
        except OrderRejected:
            transition(candidate, CandidateState.ARMED, now=snapshot.timestamp, reason="risk engine rejected entry signal")
            return

        if order.status != OrderStatus.FILLED:
            transition(candidate, CandidateState.ARMED, now=snapshot.timestamp, reason=f"entry order not filled: {order.status.value}")
            return

        transition(candidate, CandidateState.ENTERED, now=snapshot.timestamp, reason="entry order filled")
        transition(candidate, CandidateState.MANAGING, now=snapshot.timestamp, reason="position opened")

        position = next(p for p in self.broker.get_positions() if p.symbol == snapshot.symbol)
        position.stop_price = signal.suggested_stop
        position.target_price = signal.suggested_target
        position.strategy_name = signal.strategy_name

    def _execute_exit(self, candidate: Candidate, position, exit_signal, snapshot: MarketSnapshot) -> None:
        order = self.order_manager.submit_signal(exit_signal, snapshot=snapshot, position=position)
        if order.status != OrderStatus.FILLED:
            return

        fill = self.broker.poll_fills()[-1]
        pnl = (fill.price - position.avg_entry_price) * fill.quantity - fill.fees
        trade = Trade(
            symbol=position.symbol,
            strategy_name=position.strategy_name,
            side=position.side,
            entry_price=position.avg_entry_price,
            exit_price=fill.price,
            quantity=fill.quantity,
            opened_at=position.opened_at,
            closed_at=fill.filled_at,
            exit_reason=ExitReason(exit_signal.metadata["exit_reason"]),
            pnl=pnl,
            pnl_pct=(fill.price - position.avg_entry_price) / position.avg_entry_price * 100.0,
            max_favorable_excursion=position.max_favorable_excursion,
            max_adverse_excursion=position.max_adverse_excursion,
        )
        self.trades.append(trade)
        self.risk_engine.record_trade_closed(position.symbol, pnl, now=fill.filled_at)
        transition(candidate, CandidateState.EXITED, now=snapshot.timestamp, reason=trade.exit_reason.value)
        transition(candidate, CandidateState.COOLDOWN, now=snapshot.timestamp, reason="post-trade cooldown")
