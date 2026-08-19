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

from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

from ..brokers.paper.client import PaperBrokerClient, PaperBrokerConfig
from ..config import Settings, get_settings
from ..enums import CandidateState, ExitReason, OrderStatus, SignalAction
from ..execution.order_manager import OrderManager, OrderRejected
from ..interfaces.strategy import Strategy
from ..metrics.volume_profile import compute_runway_consumed_pct, evaluate_target_clearance
from ..models import Candidate, FloatData, MarketSnapshot, Signal, Trade
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
    # Entry-selectivity rework (2026-08-13, see docs/ARCHITECTURE.md and
    # runtime/trading_loop.py's matching fields): mirrored here so a
    # backtest actually predicts live behavior instead of silently reverting
    # to the old immediate-entry-on-trigger logic -- this module's docstring
    # is explicit that fidelity to the live code paths is the whole point.
    confirmation_window_seconds: float = 10.0
    confirmation_max_pullback_pct: float = 0.5
    max_runway_consumed_pct: float = 0.40


@dataclass
class _PendingConfirmation:
    signal: Signal
    started_at: datetime
    reference_price: float
    snapshot: MarketSnapshot


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
        # Same live-closure pattern as main.py's build_trading_loop -- feeds
        # CandidateWatcher's room_to_target_score MIS component the exact
        # fixed target this engine's own entries use.
        self.watcher = CandidateWatcher(
            mis_config, watcher_config,
            reward_risk_ratio_fn=lambda: self.risk_engine.config.min_risk_reward_ratio,
            stop_loss_pct_fn=lambda: self.risk_engine.config.stop_loss_pct,
        )
        self.trigger_engine = TriggerEngine(strategies)
        self.position_manager = PositionManager(position_config)
        self.candidates: dict[str, Candidate] = {}
        self.trades: list[Trade] = []
        self._pending_confirmations: dict[str, _PendingConfirmation] = {}

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

        if candidate.state == CandidateState.CONFIRMING:
            self._poll_confirmation(candidate, snapshot)
            return

        self.watcher.update(candidate, snapshot)
        signal = self.trigger_engine.on_snapshot(candidate, snapshot)
        # Roll this bar's high into resistance for the *next* bar only after
        # the trigger engine has checked it against the current level.
        self.watcher.update_resistance(candidate, snapshot)
        if signal is None:
            return

        # trigger_engine.on_snapshot is a pure "which strategy matches"
        # function (2026-08-17, see that module's docstring -- the Real-Time
        # Momentum Qualification Layer needed to run its own gate on a fired
        # signal before deciding whether CONFIRMING should even start, which
        # this backtest engine deliberately does NOT model -- see this
        # module's docstring's "NOT modeled yet" list) -- it no longer
        # transitions the candidate itself, so this does it directly.
        # Stashed for _poll_confirmation on a later bar rather than
        # submitting immediately, mirroring runtime/trading_loop.py's
        # _start_confirmation -- see this module's docstring for why a
        # backtest has to mirror the same entry-selectivity behavior live
        # trading uses (2026-08-13 rework, see docs/ARCHITECTURE.md), not a
        # simplified stand-in that would make backtest results diverge from
        # what actually happens live.
        transition(candidate, CandidateState.CONFIRMING, now=snapshot.timestamp, reason=f"{signal.strategy_name} triggered")
        self._pending_confirmations[candidate.symbol] = _PendingConfirmation(
            signal=signal, started_at=snapshot.timestamp, reference_price=signal.reference_price, snapshot=snapshot,
        )

    def _poll_confirmation(self, candidate: Candidate, snapshot: MarketSnapshot) -> None:
        """Simplified backtest counterpart to
        runtime/trading_loop.py's TradingLoop._poll_confirmation -- same
        hold-through-a-window-then-recompute-and-gate logic, minus
        cross-candidate batch ranking: bars are processed one at a time
        across a merged chronological timeline (see run()), not in
        scarce-slot-contending same-tick batches the way TradingLoop's
        single poll tick can be, so there's nothing meaningful to rank
        against here. A candidate that clears confirmation submits
        immediately; RiskEngine.evaluate's own max_simultaneous_positions
        gate still applies exactly as it would live."""
        pending = self._pending_confirmations.get(candidate.symbol)
        if pending is None:
            transition(candidate, CandidateState.ARMED, now=snapshot.timestamp, reason="no pending confirmation found for CONFIRMING candidate")
            return

        self.watcher.update(candidate, snapshot)

        elapsed_seconds = (snapshot.timestamp - pending.started_at).total_seconds()
        reversed_past_tolerance = snapshot.last_price < pending.reference_price * (
            1 - self.config.confirmation_max_pullback_pct / 100.0
        )
        mis_faded = (
            candidate.latest_score is not None
            and candidate.latest_score.score < self.watcher.config.armed_score_threshold
        )
        if reversed_past_tolerance or mis_faded:
            self._pending_confirmations.pop(candidate.symbol, None)
            reason = "price reversed during confirmation" if reversed_past_tolerance else "MIS faded during confirmation"
            transition(candidate, CandidateState.ARMED, now=snapshot.timestamp, reason=reason)
            return

        if elapsed_seconds < self.config.confirmation_window_seconds:
            return  # still waiting, nothing has failed yet

        original = pending.signal
        confirmed_entry = snapshot.last_price
        stop_pct = (
            (original.reference_price - original.suggested_stop) / original.reference_price
            if original.suggested_stop else None
        )
        target_pct = (
            (original.suggested_target - original.reference_price) / original.reference_price
            if original.suggested_target else None
        )
        confirmed_stop = confirmed_entry * (1 - stop_pct) if stop_pct is not None else original.suggested_stop
        confirmed_target = confirmed_entry * (1 + target_pct) if target_pct is not None else original.suggested_target

        target_clearance = None
        if confirmed_target is not None:
            target_clearance = evaluate_target_clearance(confirmed_entry, confirmed_target, candidate.static_resistance_levels)
            candidate.next_resistance_price = target_clearance.next_resistance
            candidate.target_clear = target_clearance.target_clear

        runway_consumed = None
        if target_clearance is not None and target_clearance.next_resistance is not None:
            runway_consumed = compute_runway_consumed_pct(
                pending.reference_price, confirmed_entry, target_clearance.next_resistance,
            )
        candidate.runway_consumed_pct = runway_consumed

        if target_clearance is not None and not target_clearance.target_clear:
            self._pending_confirmations.pop(candidate.symbol, None)
            transition(candidate, CandidateState.ARMED, now=snapshot.timestamp, reason="target sits behind known resistance")
            return

        if runway_consumed is not None and runway_consumed > self.config.max_runway_consumed_pct:
            self._pending_confirmations.pop(candidate.symbol, None)
            transition(
                candidate, CandidateState.ARMED, now=snapshot.timestamp,
                reason="confirmed entry too far into the trigger-resistance runway",
            )
            return

        self._pending_confirmations.pop(candidate.symbol, None)
        confirmed_signal = Signal(
            symbol=original.symbol, action=original.action, generated_at=snapshot.timestamp,
            strategy_name=original.strategy_name, strategy_version=original.strategy_version,
            reference_price=confirmed_entry, suggested_stop=confirmed_stop, suggested_target=confirmed_target,
            score_at_signal=candidate.latest_score.score if candidate.latest_score else original.score_at_signal,
            metadata=original.metadata,
        )
        transition(candidate, CandidateState.TRIGGERED, now=snapshot.timestamp, reason="confirmed")
        self._submit_entry(candidate, confirmed_signal, snapshot)

    def _submit_entry(self, candidate: Candidate, signal: Signal, snapshot: MarketSnapshot) -> None:
        try:
            # open_positions=self.broker.get_positions(), not omitted --
            # unlike the live TradingLoop path, this is actually correct
            # here: _process_snapshot mutates each Position object returned
            # by PaperBrokerClient.get_positions() in place right after
            # entry (position.stop_price = signal.suggested_stop below),
            # and get_positions() returns the SAME object references every
            # call (list(self._state.positions.values())), so those
            # mutations really do persist -- see submit_signal's docstring
            # for why this isn't true of the live path against either
            # broker.
            order = self.order_manager.submit_signal(
                signal, snapshot=snapshot, open_positions=self.broker.get_positions(), now=snapshot.timestamp,
            )
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
        """A SCALE_OUT signal (target hit -- see PositionManager.check_exit)
        only sells part of `position`; PaperBrokerClient._apply_fill already
        reduces the broker-side position's quantity by the fill amount
        without deleting it, so the *only* extra bookkeeping needed here is:
        don't transition the candidate to COOLDOWN, and mark
        partial_exit_taken so check_exit doesn't fire another partial next
        bar. An EXIT fully closes it (broker deletes the position once its
        quantity hits zero), same as before."""
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

        if exit_signal.action == SignalAction.SCALE_OUT:
            position.partial_exit_taken = True
            return

        transition(candidate, CandidateState.EXITED, now=snapshot.timestamp, reason=trade.exit_reason.value)
        transition(candidate, CandidateState.COOLDOWN, now=snapshot.timestamp, reason="post-trade cooldown")
