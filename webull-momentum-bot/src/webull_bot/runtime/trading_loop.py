"""
Poll-based production run-loop.

Webull streaming (subscribe_quotes) is not implemented yet -- no sandbox
MQTT host was ever confirmed (see brokers/webull/client.py). Until that
exists, this loop polls `broker.get_snapshot()` per candidate on a timer
instead of reacting to a push feed. Swapping in streaming later should only
require changing how snapshots arrive at `_process_candidate`, not the
state-machine/order logic below.

Key design point: `WebullBrokerClient.place_order` returns status=SUBMITTED,
not FILLED (a 2xx response means Webull accepted the order for processing,
confirmed live -- see that module's docstring). PaperBrokerClient, by
contrast, fills synchronously. This loop has to handle both: a freshly
submitted order parks the candidate in TRIGGERED (entries) or leaves it in
MANAGING with a pending-exit marker (exits) and polls `OrderManager.get_status`
on subsequent ticks until it resolves to FILLED or a terminal failure state.

Position tracking is intentionally NOT re-fetched from the broker every
tick: `PositionManager.check_exit` mutates trailing-stop/MFE/MAE state in
place on a Position object, and `broker.get_positions()` returns fresh
objects on every call, so re-fetching each tick would silently discard that
running state. Instead, a local Position is seeded once (from the broker,
for an accurate avg_entry_price) right when an entry fill is confirmed, and
this loop's own dict is the source of truth for it until the position closes.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

from ..enums import CandidateState, ExitReason, OrderStatus
from ..execution.order_manager import OrderManager, OrderRejected
from ..interfaces.broker import BrokerClient
from ..data.universe import SymbolUniverseProvider
from ..models import Candidate, MarketSnapshot, Order, Position, Signal, Trade
from ..position.position_manager import PositionManager
from ..risk.risk_engine import RiskEngine
from ..scanner.broad_scanner import BroadScanner
from ..scanner.candidate_watcher import CandidateWatcher
from ..scanner.trigger_engine import TriggerEngine
from ..state_machine import transition

logger = logging.getLogger(__name__)


@dataclass
class TradingLoopConfig:
    poll_interval_seconds: float = 5.0
    universe_rescan_interval_seconds: float = 60.0
    cooldown_seconds: float = 900.0  # 15 min before a cooled-down candidate can be watched again
    max_universe_size: int = 50


class TradingLoop:
    def __init__(
        self,
        broker: BrokerClient,
        universe_provider: SymbolUniverseProvider,
        broad_scanner: BroadScanner,
        watcher: CandidateWatcher,
        trigger_engine: TriggerEngine,
        order_manager: OrderManager,
        position_manager: PositionManager,
        risk_engine: RiskEngine,
        *,
        config: Optional[TradingLoopConfig] = None,
        on_trade_closed: Optional[Callable[[Trade], None]] = None,
        on_order_update: Optional[Callable[[Order], None]] = None,
    ):
        self.broker = broker
        self.universe_provider = universe_provider
        self.broad_scanner = broad_scanner
        self.watcher = watcher
        self.trigger_engine = trigger_engine
        self.order_manager = order_manager
        self.position_manager = position_manager
        self.risk_engine = risk_engine
        self.config = config or TradingLoopConfig()
        self.on_trade_closed = on_trade_closed
        # Called with every Order object this loop sees at each of its four
        # order-status touchpoints (submit entry, poll pending entry, submit
        # exit, poll pending exit) -- lets a caller persist order state
        # changes (e.g. SUBMITTED -> FILLED) without TradingLoop importing
        # the DB layer itself. May be called multiple times for the same
        # client_order_id as its status changes.
        self.on_order_update = on_order_update

        self.candidates: dict[str, Candidate] = {}
        self._entry_signals: dict[str, Signal] = {}       # symbol -> signal that triggered a pending entry
        self._pending_entry_orders: dict[str, Order] = {}  # symbol -> submitted-but-not-yet-filled entry order
        self._pending_exit_orders: dict[str, tuple[Order, Signal]] = {}  # symbol -> (order, exit signal)
        self._positions: dict[str, Position] = {}          # symbol -> our own tracked open position
        self._last_universe_scan: Optional[datetime] = None

    # -- universe / discovery ------------------------------------------------

    def _rescan_universe(self, now: datetime) -> None:
        try:
            symbols = self.universe_provider.get_symbols()[: self.config.max_universe_size]
        except Exception:
            logger.exception("Universe scan failed; keeping existing candidates this cycle.")
            return

        try:
            discovered = self.broad_scanner.scan(symbols)
        except Exception:
            logger.exception("BroadScanner.scan failed.")
            return

        for candidate in discovered:
            if candidate.symbol not in self.candidates:
                self.candidates[candidate.symbol] = candidate

    # -- per-candidate processing ---------------------------------------------

    def _process_candidate(self, candidate: Candidate, now: datetime) -> None:
        if candidate.state == CandidateState.REJECTED:
            return

        if candidate.state == CandidateState.COOLDOWN:
            if now - candidate.last_updated_at >= timedelta(seconds=self.config.cooldown_seconds):
                transition(candidate, CandidateState.WATCHING, now=now, reason="cooldown expired")
            return

        try:
            snapshot = self.broker.get_snapshot(candidate.symbol)
        except Exception:
            logger.warning("get_snapshot failed for %s this cycle; skipping.", candidate.symbol, exc_info=True)
            return

        if candidate.state == CandidateState.TRIGGERED:
            self._poll_pending_entry(candidate, now)
            return

        if candidate.state in (CandidateState.ENTERED, CandidateState.MANAGING):
            self._manage_position(candidate, snapshot, now)
            return

        # DISCOVERED / WATCHING / HEATING_UP / ARMED
        self.watcher.update(candidate, snapshot)
        signal = self.trigger_engine.on_snapshot(candidate, snapshot)
        # Roll this bar's high into resistance only AFTER the trigger engine
        # has checked it against the pre-bar level (see candidate_watcher.py).
        self.watcher.update_resistance(candidate, snapshot)

        if signal is None:
            return
        self._submit_entry(candidate, signal, snapshot, now)

    def _notify_order_update(self, order: Order) -> None:
        if self.on_order_update is not None:
            try:
                self.on_order_update(order)
            except Exception:
                logger.exception("on_order_update callback raised for order %s.", order.client_order_id)

    def _submit_entry(self, candidate: Candidate, signal: Signal, snapshot: MarketSnapshot, now: datetime) -> None:
        try:
            order = self.order_manager.submit_signal(signal, snapshot=snapshot)
        except OrderRejected as exc:
            transition(candidate, CandidateState.ARMED, now=now, reason=f"risk engine rejected entry: {exc.decision.reason}")
            return
        self._notify_order_update(order)

        if order.status == OrderStatus.FILLED:
            self._confirm_entry_filled(candidate, signal, order, now)
        elif order.status in (OrderStatus.SUBMITTED, OrderStatus.ACCEPTED, OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED):
            self._entry_signals[candidate.symbol] = signal
            self._pending_entry_orders[candidate.symbol] = order
            # trigger_engine already moved this candidate to TRIGGERED.
        else:
            transition(candidate, CandidateState.ARMED, now=now, reason=f"entry order {order.status.value}")

    def _poll_pending_entry(self, candidate: Candidate, now: datetime) -> None:
        pending = self._pending_entry_orders.get(candidate.symbol)
        if pending is None:
            # Shouldn't happen, but don't get stuck in TRIGGERED forever.
            transition(candidate, CandidateState.ARMED, now=now, reason="no pending order found for TRIGGERED candidate")
            return

        try:
            status_order = self.order_manager.get_status(pending.broker_order_id)
        except Exception:
            logger.warning("get_order_status failed for %s this cycle.", candidate.symbol, exc_info=True)
            return
        self._notify_order_update(status_order)

        if status_order.status == OrderStatus.FILLED:
            signal = self._entry_signals.pop(candidate.symbol)
            self._pending_entry_orders.pop(candidate.symbol, None)
            self._confirm_entry_filled(candidate, signal, status_order, now)
        elif status_order.status in (OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED):
            self._entry_signals.pop(candidate.symbol, None)
            self._pending_entry_orders.pop(candidate.symbol, None)
            transition(candidate, CandidateState.ARMED, now=now, reason=f"entry order {status_order.status.value}")
        # else still pending: leave as TRIGGERED, check again next tick

    def _confirm_entry_filled(self, candidate: Candidate, signal: Signal, order: Order, now: datetime) -> None:
        avg_entry_price = signal.reference_price
        quantity = order.quantity
        try:
            live_position = next(p for p in self.broker.get_positions() if p.symbol == candidate.symbol)
            avg_entry_price = live_position.avg_entry_price
            quantity = live_position.quantity
        except StopIteration:
            logger.warning(
                "No broker position found for %s immediately after fill confirmation; "
                "using signal reference price %.4f as avg_entry_price.",
                candidate.symbol, avg_entry_price,
            )

        self._positions[candidate.symbol] = Position(
            symbol=candidate.symbol,
            side=order.side,
            quantity=quantity,
            avg_entry_price=avg_entry_price,
            stop_price=signal.suggested_stop,
            target_price=signal.suggested_target,
            trailing_stop_pct=None,
            opened_at=now,
            strategy_name=signal.strategy_name,
        )
        transition(candidate, CandidateState.ENTERED, now=now, reason="entry order filled")
        transition(candidate, CandidateState.MANAGING, now=now, reason="managing open position")

    def _manage_position(self, candidate: Candidate, snapshot: MarketSnapshot, now: datetime) -> None:
        pending = self._pending_exit_orders.get(candidate.symbol)
        if pending is not None:
            self._poll_pending_exit(candidate, snapshot, now)
            return

        position = self._positions.get(candidate.symbol)
        if position is None:
            logger.warning("%s is %s but has no tracked position; moving to COOLDOWN.", candidate.symbol, candidate.state.value)
            transition(candidate, CandidateState.EXITED, now=now, reason="position tracking lost")
            transition(candidate, CandidateState.COOLDOWN, now=now, reason="post-trade cooldown")
            return

        exit_signal = self.position_manager.check_exit(position, snapshot, now=now)
        if exit_signal is None:
            return

        try:
            order = self.order_manager.submit_signal(exit_signal, snapshot=snapshot, position=position)
        except OrderRejected:
            # Exits aren't supposed to be rejectable (see order_manager.py),
            # but don't crash the loop if something unexpected happens.
            logger.exception("Unexpected OrderRejected on an exit signal for %s.", candidate.symbol)
            return
        self._notify_order_update(order)

        if order.status == OrderStatus.FILLED:
            self._finalize_exit(candidate, position, order, exit_signal, now)
        else:
            self._pending_exit_orders[candidate.symbol] = (order, exit_signal)

    def _poll_pending_exit(self, candidate: Candidate, snapshot: MarketSnapshot, now: datetime) -> None:
        order, exit_signal = self._pending_exit_orders[candidate.symbol]
        try:
            status_order = self.order_manager.get_status(order.broker_order_id)
        except Exception:
            logger.warning("get_order_status failed for pending exit on %s.", candidate.symbol, exc_info=True)
            return
        self._notify_order_update(status_order)

        if status_order.status == OrderStatus.FILLED:
            self._pending_exit_orders.pop(candidate.symbol)
            position = self._positions[candidate.symbol]
            self._finalize_exit(candidate, position, status_order, exit_signal, now)
        elif status_order.status in (OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED):
            self._pending_exit_orders.pop(candidate.symbol)
            logger.warning("Exit order for %s did not fill (%s); will re-evaluate exit next tick.", candidate.symbol, status_order.status.value)
        # else still pending

    def _finalize_exit(self, candidate: Candidate, position: Position, order: Order, exit_signal: Signal, now: datetime) -> None:
        exit_price = None
        try:
            fills = [f for f in self.broker.poll_fills(since=order.created_at) if f.order_client_id == order.client_order_id]
            if fills:
                exit_price = fills[-1].price
        except Exception:
            logger.warning("poll_fills failed while finalizing exit for %s.", candidate.symbol, exc_info=True)

        if exit_price is None:
            # Fallback: fill lookup is best-effort (see WebullBrokerClient's
            # poll_fills docstring -- unverified response shape). Approximate
            # with the position's stop/target level rather than fabricating precision.
            exit_price = position.stop_price or position.target_price or position.avg_entry_price

        pnl = (exit_price - position.avg_entry_price) * order.quantity
        pnl_pct = (exit_price - position.avg_entry_price) / position.avg_entry_price * 100.0 if position.avg_entry_price else 0.0
        exit_reason = ExitReason(exit_signal.metadata.get("exit_reason", ExitReason.MANUAL.value))

        trade = Trade(
            symbol=candidate.symbol,
            strategy_name=position.strategy_name,
            side=position.side,
            entry_price=position.avg_entry_price,
            exit_price=exit_price,
            quantity=order.quantity,
            opened_at=position.opened_at,
            closed_at=now,
            exit_reason=exit_reason,
            pnl=pnl,
            pnl_pct=pnl_pct,
            max_favorable_excursion=position.max_favorable_excursion,
            max_adverse_excursion=position.max_adverse_excursion,
        )

        self.risk_engine.record_trade_closed(candidate.symbol, pnl, now=now)
        self._positions.pop(candidate.symbol, None)
        if self.on_trade_closed is not None:
            try:
                self.on_trade_closed(trade)
            except Exception:
                logger.exception("on_trade_closed callback raised for %s.", candidate.symbol)

        transition(candidate, CandidateState.EXITED, now=now, reason=exit_reason.value)
        transition(candidate, CandidateState.COOLDOWN, now=now, reason="post-trade cooldown")

    # -- read-only accessors for external consumers (e.g. the dashboard) -----

    def get_candidates(self) -> dict[str, Candidate]:
        """Shallow copy of the tracked candidates dict -- safe to iterate
        without racing a concurrent run_once() mutating it (e.g. from a
        dashboard reading this loop's state from another thread)."""
        return dict(self.candidates)

    def get_open_positions(self) -> dict[str, Position]:
        return dict(self._positions)

    # -- main loop -------------------------------------------------------------

    def run_once(self, now: Optional[datetime] = None) -> None:
        now = now or datetime.utcnow()
        if (
            self._last_universe_scan is None
            or (now - self._last_universe_scan) >= timedelta(seconds=self.config.universe_rescan_interval_seconds)
        ):
            self._rescan_universe(now)
            self._last_universe_scan = now

        for candidate in list(self.candidates.values()):
            try:
                self._process_candidate(candidate, now)
            except Exception:
                logger.exception("Unhandled error processing candidate %s; continuing loop.", candidate.symbol)

    def run_forever(self, stop_flag: Optional[Callable[[], bool]] = None) -> None:
        """Runs run_once() on a timer until stop_flag() returns True (if provided)."""
        while stop_flag is None or not stop_flag():
            self.run_once()
            time.sleep(self.config.poll_interval_seconds)
