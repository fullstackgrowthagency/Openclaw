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

from ..collection.event_recorder import MomentumEventTracker
from ..enums import CandidateState, ExitReason, OrderStatus
from ..execution.order_manager import OrderManager, OrderRejected
from ..interfaces.broker import BrokerClient
from ..data.universe import SymbolUniverseProvider
from ..models import Candidate, MarketSnapshot, MomentumEvent, MomentumScore, Order, Position, Signal, Trade
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
    # Webull's sandbox enforces a real sustained rate limit paced globally
    # by webull_market_data_limiter (1.0s minimum interval) regardless of
    # BroadScanner's concurrency -- see brokers/webull/retry.py's module
    # docstring for the live discovery process, which took several rounds
    # to get right (a rate limiter that only paced the first attempt of
    # each call, not retries, still produced hard failures under real
    # concurrent load). A live end-to-end test after that fix -- 100 real
    # symbols from MultiSourceUniverseProvider, 10 concurrent workers,
    # get_snapshot against the actual sandbox -- completed with **zero**
    # hard failures (some individual 429s, all recovered by the paced
    # retry) but took 124.9s, i.e. ~1.25s/symbol, not the naive 1.0s the
    # limiter's own interval would suggest -- occasional retries add real
    # time on top.
    #
    # BroadScanner._passes_average_volume_filter (scanner/broad_scanner.py)
    # later added a *second* Webull-paced call per symbol
    # (get_daily_volumes), re-measured live at 60 symbols -> 171.9s, i.e.
    # ~2.86s/symbol -- more than double the single-call figure above, not
    # just 2x, since occasional retries apply per call. max_universe_size
    # below was cut roughly in proportion (100 * 1.25/2.86 ~= 44, rounded to
    # 45) to keep the same rescan cadence rather than letting scans stretch
    # to 4-5 minutes -- for a momentum bot, missing a fast mover for that
    # long defeats the point of frequent rescanning, so breadth was traded
    # down in favor of keeping cadence tight. 190s leaves ~45% margin over
    # 45 symbols' measured ~129s (45 * 2.86s), consistent with the margin
    # used before this filter existed, rather than sizing to a best case.
    universe_rescan_interval_seconds: float = 190.0
    cooldown_seconds: float = 900.0  # 15 min before a cooled-down candidate can be watched again
    # Raised from the original 50 now that BroadScanner checks symbols
    # concurrently (see scanner/broad_scanner.py) and the 3-source universe
    # can return ~150-250 unique symbols before this cap is applied. Cut
    # back down from 100 to 45 once the average-volume filter added a
    # second per-symbol Webull call -- see universe_rescan_interval_seconds
    # above for the measured numbers behind that. Going higher (e.g. 100+)
    # would extrapolate past what's actually been measured live rather
    # than build on it, and would force stretching the rescan interval even
    # further, delaying candidate management (which runs right after the
    # rescan in the same run_once() call) on every cycle.
    max_universe_size: int = 45


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
        on_state_transition: Optional[Callable[[str, CandidateState, CandidateState, datetime], None]] = None,
        on_score_computed: Optional[Callable[[str, MomentumScore], None]] = None,
        momentum_event_tracker: Optional[MomentumEventTracker] = None,
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
        # Called once per state-machine transition (symbol, from_state,
        # to_state, timestamp), diffed off Candidate.state_history at the end
        # of every _process_candidate() call -- see _flush_state_transitions.
        # This covers transitions made anywhere (watcher, trigger_engine, or
        # this class itself) without those modules needing to know about it.
        self.on_state_transition = on_state_transition
        # Called with the freshly computed MomentumScore every time
        # CandidateWatcher.update() produces one.
        self.on_score_computed = on_score_computed
        # Optional collaborator (not a callback) since momentum-event
        # tracking needs ongoing state across many ticks (filling forward-
        # looking outcome windows over up to 15 minutes) -- see
        # collection/event_recorder.py.
        self.momentum_event_tracker = momentum_event_tracker

        self.candidates: dict[str, Candidate] = {}
        self._entry_signals: dict[str, Signal] = {}       # symbol -> signal that triggered a pending entry
        self._pending_entry_orders: dict[str, Order] = {}  # symbol -> submitted-but-not-yet-filled entry order
        self._pending_exit_orders: dict[str, tuple[Order, Signal]] = {}  # symbol -> (order, exit signal)
        self._positions: dict[str, Position] = {}          # symbol -> our own tracked open position
        self._last_universe_scan: Optional[datetime] = None
        self._persisted_transition_counts: dict[str, int] = {}  # symbol -> len(state_history) already flushed

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
        """Thin wrapper that guarantees _flush_state_transitions runs exactly
        once per tick regardless of which branch below returns early --
        _process_candidate_inner uses plain `return` freely."""
        try:
            self._process_candidate_inner(candidate, now)
        finally:
            self._flush_state_transitions(candidate)

    def _process_candidate_inner(self, candidate: Candidate, now: datetime) -> None:
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

        if self.momentum_event_tracker is not None:
            try:
                self.momentum_event_tracker.on_snapshot(candidate.symbol, snapshot)
            except Exception:
                logger.exception("momentum_event_tracker.on_snapshot failed for %s.", candidate.symbol)

        if candidate.state == CandidateState.TRIGGERED:
            self._poll_pending_entry(candidate, now)
            return

        if candidate.state in (CandidateState.ENTERED, CandidateState.MANAGING):
            self._manage_position(candidate, snapshot, now)
            return

        # DISCOVERED / WATCHING / HEATING_UP / ARMED
        self.watcher.update(candidate, snapshot)
        self._notify_score(candidate)
        signal = self.trigger_engine.on_snapshot(candidate, snapshot)
        # Roll this bar's high into resistance only AFTER the trigger engine
        # has checked it against the pre-bar level (see candidate_watcher.py).
        self.watcher.update_resistance(candidate, snapshot)

        if signal is None:
            return
        momentum_event = self._register_momentum_event(candidate, signal, now)
        self._submit_entry(candidate, signal, snapshot, now, momentum_event=momentum_event)

    def _notify_score(self, candidate: Candidate) -> None:
        if self.on_score_computed is not None and candidate.latest_score is not None:
            try:
                self.on_score_computed(candidate.symbol, candidate.latest_score)
            except Exception:
                logger.exception("on_score_computed callback raised for %s.", candidate.symbol)

    def _flush_state_transitions(self, candidate: Candidate) -> None:
        if self.on_state_transition is None:
            return
        already_persisted = self._persisted_transition_counts.get(candidate.symbol, 0)
        history = candidate.state_history
        total = len(history)
        if total <= already_persisted:
            return
        for i in range(already_persisted, total):
            from_state, timestamp = history[i]
            to_state = history[i + 1][0] if i + 1 < total else candidate.state
            try:
                self.on_state_transition(candidate.symbol, from_state, to_state, timestamp)
            except Exception:
                logger.exception("on_state_transition callback raised for %s.", candidate.symbol)
        self._persisted_transition_counts[candidate.symbol] = total

    def _register_momentum_event(self, candidate: Candidate, signal: Signal, now: datetime) -> Optional[MomentumEvent]:
        if self.momentum_event_tracker is None:
            return None
        event = MomentumEvent(
            symbol=candidate.symbol,
            detected_at=now,
            trigger_reason=f"{signal.strategy_name}:{signal.action.value}",
            was_traded=False,  # flipped to True in _submit_entry if the order actually gets submitted
            score_at_event=candidate.latest_score.score if candidate.latest_score else None,
            metrics_at_event=candidate.latest_metrics,
            price_at_event=signal.reference_price,
        )
        try:
            self.momentum_event_tracker.register(event)
        except Exception:
            logger.exception("momentum_event_tracker.register failed for %s.", candidate.symbol)
            return None
        return event

    def _notify_order_update(self, order: Order) -> None:
        if self.on_order_update is not None:
            try:
                self.on_order_update(order)
            except Exception:
                logger.exception("on_order_update callback raised for order %s.", order.client_order_id)

    def _submit_entry(
        self, candidate: Candidate, signal: Signal, snapshot: MarketSnapshot, now: datetime,
        momentum_event: Optional[MomentumEvent] = None,
    ) -> None:
        try:
            order = self.order_manager.submit_signal(signal, snapshot=snapshot)
        except OrderRejected as exc:
            transition(candidate, CandidateState.ARMED, now=now, reason=f"risk engine rejected entry: {exc.decision.reason}")
            return
        if momentum_event is not None:
            # Mutating in place is enough -- the tracker holds this same
            # object and will persist the change on its next on_snapshot()
            # call for this symbol (see _register_momentum_event).
            momentum_event.was_traded = True
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
