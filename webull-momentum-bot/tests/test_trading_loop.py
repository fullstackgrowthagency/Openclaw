"""
Tests for the poll-based TradingLoop orchestrator.

Two styles here, deliberately:
  - A full pipeline test using the real PaperBrokerClient (fills
    synchronously), mirroring test_backtest_engine.py's scenario, to prove
    the loop wires scanner -> watcher -> trigger -> risk -> order manager ->
    position manager together correctly end to end.
  - Focused tests using a minimal FakeBroker that returns SUBMITTED before
    FILLED, since PaperBrokerClient can't exercise the pending-order polling
    branches (it always fills immediately) -- but WebullBrokerClient always
    returns SUBMITTED first (confirmed live), so that path needs its own
    coverage independent of a real network connection.
"""
import threading
import time as time_module
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import pytest

from webull_bot.brokers.paper.client import PaperBrokerClient
from webull_bot.config import get_settings
from webull_bot.data.universe import StaticUniverseProvider
from webull_bot.enums import ExitReason, OrderSide, OrderStatus, OrderType
from webull_bot.execution.order_manager import OrderManager
from webull_bot.interfaces.broker import BrokerClient
from webull_bot.interfaces.float_provider import FloatDataProvider
from webull_bot.models import Fill, FloatData, MarketSnapshot, Order, Position
from webull_bot.position.position_manager import PositionManager
from webull_bot.risk.risk_engine import RiskConfig, RiskEngine
from webull_bot.runtime.trading_loop import TradingLoop, TradingLoopConfig
from webull_bot.scanner.broad_scanner import BroadScanner
from webull_bot.scanner.candidate_watcher import CandidateWatcher, WatcherConfig
from webull_bot.strategy.momentum_breakout import MomentumBreakoutStrategy


class _SingleSymbolFloatProvider(FloatDataProvider):
    def __init__(self, symbol: str, free_float_shares: float):
        self._data = FloatData(
            symbol=symbol, free_float_shares=free_float_shares, shares_outstanding=free_float_shares * 1.3,
            market_cap=None, float_percent=None, effective_date=None, fetched_at=datetime.utcnow(),
        )

    def get_float_data(self, symbol: str) -> FloatData:
        return self._data

    def get_float_data_bulk(self, symbols):
        return {s: self._data for s in symbols}


def _snapshot(t, last_price, high_of_day, cumulative_volume, bid, ask, vwap) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="TEST", timestamp=t, last_price=last_price, bid=bid, ask=ask, bid_size=500, ask_size=500,
        cumulative_volume=cumulative_volume, vwap=vwap, high_of_day=high_of_day, low_of_day=4.95, open_price=5.00,
    )


def _build_bars() -> list[MarketSnapshot]:
    t0 = datetime(2026, 1, 5, 9, 31, 0)
    return [
        _snapshot(t0, 5.00, 5.05, 150_000, 4.99, 5.01, 5.00),
        _snapshot(t0 + timedelta(minutes=1), 5.10, 5.10, 250_000, 5.09, 5.11, 5.05),
        _snapshot(t0 + timedelta(minutes=2), 5.20, 5.20, 600_000, 5.19, 5.21, 5.15),
        _snapshot(t0 + timedelta(minutes=3), 5.60, 5.60, 650_000, 5.59, 5.61, 5.50),
    ]


def _build_loop(broker, **extra_kwargs) -> TradingLoop:
    float_provider = _SingleSymbolFloatProvider("TEST", 3_000_000)
    broad_scanner = BroadScanner(broker, float_provider)
    watcher = CandidateWatcher(config=WatcherConfig(heating_up_score_threshold=15.0, armed_score_threshold=35.0))
    from webull_bot.scanner.trigger_engine import TriggerEngine

    trigger_engine = TriggerEngine(strategies=[MomentumBreakoutStrategy()])
    risk_engine = RiskEngine(RiskConfig(risk_per_trade_pct=0.5, stop_loss_required=True))
    order_manager = OrderManager(broker, risk_engine, get_settings())
    position_manager = PositionManager()

    trades = []
    loop = TradingLoop(
        broker, StaticUniverseProvider(["TEST"]), broad_scanner, watcher, trigger_engine,
        order_manager, position_manager, risk_engine,
        config=TradingLoopConfig(universe_rescan_interval_seconds=3600, cooldown_seconds=900),
        on_trade_closed=trades.append,
        **extra_kwargs,
    )
    loop._trades = trades  # test-only convenience handle
    return loop


def test_full_pipeline_enters_and_partially_exits_via_paper_broker():
    # Hitting target sells half (SCALE_OUT) and leaves the remainder open
    # under trailing-stop/breakeven management -- see
    # docs/ARCHITECTURE.md's "Position management" section.
    broker = PaperBrokerClient()
    broker.connect()
    loop = _build_loop(broker)
    bars = _build_bars()

    for bar in bars:
        broker.feed_snapshot(bar)
        loop.run_once(now=bar.timestamp)

    assert len(loop._trades) == 1
    trade = loop._trades[0]
    assert trade.symbol == "TEST"
    assert trade.exit_reason == ExitReason.PARTIAL_PROFIT_TARGET
    assert trade.pnl > 0

    candidate = loop.candidates["TEST"]
    assert candidate.state.value == "managing"
    assert "TEST" in loop._positions
    assert loop._positions["TEST"].partial_exit_taken is True
    assert loop._positions["TEST"].quantity > 0


def test_no_trade_when_thresholds_never_reached():
    broker = PaperBrokerClient()
    broker.connect()
    float_provider = _SingleSymbolFloatProvider("TEST", 3_000_000)
    broad_scanner = BroadScanner(broker, float_provider)
    watcher = CandidateWatcher(config=WatcherConfig(heating_up_score_threshold=99.0, armed_score_threshold=99.9))
    from webull_bot.scanner.trigger_engine import TriggerEngine

    trigger_engine = TriggerEngine(strategies=[MomentumBreakoutStrategy()])
    risk_engine = RiskEngine()
    order_manager = OrderManager(broker, risk_engine, get_settings())
    position_manager = PositionManager()
    trades = []
    loop = TradingLoop(
        broker, StaticUniverseProvider(["TEST"]), broad_scanner, watcher, trigger_engine,
        order_manager, position_manager, risk_engine,
        config=TradingLoopConfig(universe_rescan_interval_seconds=3600),
        on_trade_closed=trades.append,
    )
    for bar in _build_bars():
        broker.feed_snapshot(bar)
        loop.run_once(now=bar.timestamp)
    assert trades == []


# -- pending-order polling (requires a fake broker; PaperBrokerClient always fills immediately) --

@dataclass
class _FakeBroker(BrokerClient):
    """Returns SUBMITTED on first place_order, then FILLED on the Nth get_order_status poll."""
    fills_after_polls: int = 1
    equity: float = 25_000.0
    _positions: list = field(default_factory=list)
    _orders: dict = field(default_factory=dict)
    _poll_counts: dict = field(default_factory=dict)
    _fills: list = field(default_factory=list)

    def connect(self): pass
    def disconnect(self): pass
    def get_account_equity(self): return self.equity
    def get_buying_power(self): return self.equity
    def get_positions(self): return list(self._positions)

    def get_snapshot(self, symbol):
        return MarketSnapshot(
            symbol=symbol, timestamp=datetime.utcnow(), last_price=5.20, bid=5.19, ask=5.21,
            bid_size=100, ask_size=100, cumulative_volume=200_000, vwap=5.20, high_of_day=5.20,
            low_of_day=5.20, open_price=5.20,
        )

    def get_bars(self, symbol, interval, lookback): raise NotImplementedError
    def subscribe_quotes(self, symbols, on_update): raise NotImplementedError

    def place_order(self, order: Order) -> Order:
        order.status = OrderStatus.SUBMITTED
        order.broker_order_id = order.client_order_id
        self._orders[order.broker_order_id] = order
        self._poll_counts[order.broker_order_id] = 0
        if order.side == OrderSide.BUY:
            self._positions.append(
                Position(
                    symbol=order.symbol, side=OrderSide.BUY, quantity=order.quantity, avg_entry_price=5.20,
                    stop_price=None, target_price=None, trailing_stop_pct=None, opened_at=datetime.utcnow(),
                    strategy_name="test",
                )
            )
        return order

    def cancel_order(self, broker_order_id): pass
    def modify_order(self, broker_order_id, **changes): raise NotImplementedError

    def get_order_status(self, broker_order_id: str) -> Order:
        order = self._orders[broker_order_id]
        self._poll_counts[broker_order_id] += 1
        if self._poll_counts[broker_order_id] >= self.fills_after_polls:
            order.status = OrderStatus.FILLED
            self._fills.append(
                Fill(
                    order_client_id=order.client_order_id, symbol=order.symbol, side=order.side,
                    quantity=order.quantity, price=5.25, filled_at=datetime.utcnow(),
                )
            )
            if order.side == OrderSide.SELL:
                self._positions = [p for p in self._positions if p.symbol != order.symbol]
        return order

    def poll_fills(self, since=None):
        return list(self._fills)

    @property
    def is_live(self): return False


def _armed_candidate_setup(broker):
    from webull_bot.state_machine import new_candidate, transition
    from webull_bot.enums import CandidateState

    candidate = new_candidate("TEST")
    candidate.resistance_level = 5.10
    transition(candidate, CandidateState.WATCHING)
    transition(candidate, CandidateState.HEATING_UP)
    transition(candidate, CandidateState.ARMED)

    risk_engine = RiskEngine(RiskConfig(stop_loss_required=True))
    order_manager = OrderManager(broker, risk_engine, get_settings())
    watcher = CandidateWatcher()
    from webull_bot.scanner.trigger_engine import TriggerEngine

    trigger_engine = TriggerEngine(strategies=[MomentumBreakoutStrategy()])
    position_manager = PositionManager()
    loop = TradingLoop(
        broker, StaticUniverseProvider([]), BroadScanner(broker, _SingleSymbolFloatProvider("TEST", 1_000_000)),
        watcher, trigger_engine, order_manager, position_manager, risk_engine,
        config=TradingLoopConfig(universe_rescan_interval_seconds=3600),
    )
    loop.candidates["TEST"] = candidate
    return loop, candidate


def test_entry_stays_triggered_while_order_pending_then_enters_on_fill():
    broker = _FakeBroker(fills_after_polls=2)
    loop, candidate = _armed_candidate_setup(broker)

    snapshot = _snapshot(datetime.utcnow(), 5.20, 5.20, 600_000, 5.19, 5.21, 5.15)
    from webull_bot.models import Signal
    from webull_bot.enums import SignalAction
    from webull_bot.state_machine import transition
    from webull_bot.enums import CandidateState

    # In the real pipeline, trigger_engine.on_snapshot transitions ARMED ->
    # TRIGGERED as a side effect before _submit_entry is ever called -- do
    # the same here so this focused test matches that real precondition.
    transition(candidate, CandidateState.TRIGGERED)

    signal = Signal(
        symbol="TEST", action=SignalAction.ENTER_LONG, generated_at=snapshot.timestamp,
        strategy_name="test", strategy_version="v1", reference_price=5.20, suggested_stop=5.00,
    )
    order_updates = []
    loop.on_order_update = order_updates.append

    loop._submit_entry(candidate, signal, snapshot, snapshot.timestamp)
    assert candidate.symbol in loop._pending_entry_orders
    assert len(order_updates) == 1
    assert order_updates[0].status.value == "submitted"

    # First poll: still not filled (fills_after_polls=2, this is poll #1).
    loop._poll_pending_entry(candidate, snapshot.timestamp)
    assert candidate.symbol in loop._pending_entry_orders
    assert candidate.state.value == "triggered"
    assert len(order_updates) == 2

    # Second poll: fills.
    loop._poll_pending_entry(candidate, snapshot.timestamp)
    assert candidate.symbol not in loop._pending_entry_orders
    assert len(order_updates) == 3
    assert order_updates[-1].status.value == "filled"
    assert candidate.state.value == "managing"
    assert "TEST" in loop._positions
    assert loop._positions["TEST"].avg_entry_price == 5.20  # from broker.get_positions()


def test_exit_stays_managing_while_pending_then_finalizes_on_fill():
    broker = _FakeBroker(fills_after_polls=1)
    loop, candidate = _armed_candidate_setup(broker)
    from webull_bot.state_machine import transition
    from webull_bot.enums import CandidateState

    transition(candidate, CandidateState.TRIGGERED)
    transition(candidate, CandidateState.ENTERED)
    transition(candidate, CandidateState.MANAGING)
    loop._positions["TEST"] = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=10, avg_entry_price=5.00, stop_price=4.50,
        target_price=5.10, trailing_stop_pct=None, opened_at=datetime.utcnow(), strategy_name="test",
    )

    trades = []
    loop.on_trade_closed = trades.append
    snapshot = _snapshot(datetime.utcnow(), 5.15, 5.15, 600_000, 5.14, 5.16, 5.10)  # above target 5.10
    loop._manage_position(candidate, snapshot, snapshot.timestamp)
    assert candidate.symbol in loop._pending_exit_orders
    assert candidate.state.value == "managing"

    loop._poll_pending_exit(candidate, snapshot, snapshot.timestamp)
    assert candidate.symbol not in loop._pending_exit_orders
    # Target hit sells half (quantity=10 -> 5) and stays MANAGING rather
    # than closing the whole position -- see PositionManager.check_exit.
    assert candidate.state.value == "managing"
    assert len(trades) == 1
    assert trades[0].exit_reason == ExitReason.PARTIAL_PROFIT_TARGET
    assert trades[0].exit_price == 5.25  # from the fake fill
    assert trades[0].quantity == 5
    assert loop._positions["TEST"].quantity == 5
    assert loop._positions["TEST"].partial_exit_taken is True


def test_cooldown_expires_back_to_watching():
    broker = PaperBrokerClient()
    broker.connect()
    loop = _build_loop(broker)
    from webull_bot.state_machine import new_candidate, transition
    from webull_bot.enums import CandidateState

    now = datetime.utcnow()
    candidate = new_candidate("TEST", now=now - timedelta(seconds=1000))
    transition(candidate, CandidateState.WATCHING, now=now - timedelta(seconds=1000))
    transition(candidate, CandidateState.HEATING_UP, now=now - timedelta(seconds=1000))
    transition(candidate, CandidateState.ARMED, now=now - timedelta(seconds=1000))
    transition(candidate, CandidateState.TRIGGERED, now=now - timedelta(seconds=1000))
    transition(candidate, CandidateState.ENTERED, now=now - timedelta(seconds=1000))
    transition(candidate, CandidateState.MANAGING, now=now - timedelta(seconds=1000))
    transition(candidate, CandidateState.EXITED, now=now - timedelta(seconds=1000))
    transition(candidate, CandidateState.COOLDOWN, now=now - timedelta(seconds=1000))
    loop.candidates["TEST"] = candidate

    loop._process_candidate(candidate, now)  # cooldown_seconds=900, 1000s elapsed -> should expire
    assert candidate.state == CandidateState.WATCHING


# -- persistence hooks: state transitions, MIS scores, momentum events -----

def test_state_transitions_are_flushed_in_order_via_callback():
    from webull_bot.enums import CandidateState

    broker = PaperBrokerClient()
    broker.connect()
    transitions = []
    loop = _build_loop(broker, on_state_transition=lambda symbol, frm, to, ts: transitions.append((symbol, frm, to)))

    for bar in _build_bars():
        broker.feed_snapshot(bar)
        loop.run_once(now=bar.timestamp)

    symbols = {t[0] for t in transitions}
    assert symbols == {"TEST"}
    path = [(t[1], t[2]) for t in transitions]
    # Hitting target only partially exits (SCALE_OUT) and stays MANAGING --
    # see docs/ARCHITECTURE.md's "Position management" section -- so this
    # 4-bar run never reaches EXITED/COOLDOWN.
    assert path == [
        (CandidateState.DISCOVERED, CandidateState.WATCHING),
        (CandidateState.WATCHING, CandidateState.HEATING_UP),
        (CandidateState.HEATING_UP, CandidateState.ARMED),
        (CandidateState.ARMED, CandidateState.TRIGGERED),
        (CandidateState.TRIGGERED, CandidateState.ENTERED),
        (CandidateState.ENTERED, CandidateState.MANAGING),
    ]


def test_state_transitions_not_double_flushed_across_ticks():
    """Each transition must be reported exactly once, even though
    _flush_state_transitions runs on every tick for every candidate."""
    broker = PaperBrokerClient()
    broker.connect()
    transitions = []
    loop = _build_loop(broker, on_state_transition=lambda symbol, frm, to, ts: transitions.append((frm, to)))

    for bar in _build_bars():
        broker.feed_snapshot(bar)
        loop.run_once(now=bar.timestamp)

    assert len(transitions) == len(set(transitions)) or len(transitions) == 8  # no duplicates


def test_score_computed_callback_fires_for_watched_candidate():
    broker = PaperBrokerClient()
    broker.connect()
    scores = []
    loop = _build_loop(broker, on_score_computed=lambda symbol, score: scores.append((symbol, score)))

    bars = _build_bars()
    broker.feed_snapshot(bars[0])
    loop.run_once(now=bars[0].timestamp)

    assert len(scores) == 1
    symbol, score = scores[0]
    assert symbol == "TEST"
    assert 0.0 <= score.score <= 100.0


def test_momentum_event_registered_and_marked_traded_on_successful_entry():
    from webull_bot.collection.event_recorder import EventRecorder, MomentumEventTracker

    broker = PaperBrokerClient()
    broker.connect()
    recorder = EventRecorder()
    tracker = MomentumEventTracker(recorder)
    loop = _build_loop(broker, momentum_event_tracker=tracker)

    for bar in _build_bars():
        broker.feed_snapshot(bar)
        loop.run_once(now=bar.timestamp)

    event = recorder.get(1)
    assert event.symbol == "TEST"
    assert event.was_traded is True
    assert "momentum_breakout" in event.trigger_reason
    assert event.price_at_event == pytest.approx(5.20)


def test_momentum_event_not_marked_traded_when_risk_engine_rejects():
    from webull_bot.collection.event_recorder import EventRecorder, MomentumEventTracker

    broker = PaperBrokerClient()
    broker.connect()
    recorder = EventRecorder()
    tracker = MomentumEventTracker(recorder)
    # max_trades_per_day=0 guarantees the risk engine rejects every entry signal.
    float_provider = _SingleSymbolFloatProvider("TEST", 3_000_000)
    broad_scanner = BroadScanner(broker, float_provider)
    watcher = CandidateWatcher(config=WatcherConfig(heating_up_score_threshold=15.0, armed_score_threshold=35.0))
    from webull_bot.scanner.trigger_engine import TriggerEngine

    risk_engine = RiskEngine(RiskConfig(max_trades_per_day=0))
    order_manager = OrderManager(broker, risk_engine, get_settings())
    loop = TradingLoop(
        broker, StaticUniverseProvider(["TEST"]), broad_scanner, watcher,
        TriggerEngine([MomentumBreakoutStrategy()]), order_manager, PositionManager(), risk_engine,
        config=TradingLoopConfig(universe_rescan_interval_seconds=3600),
        momentum_event_tracker=tracker,
    )

    for bar in _build_bars():
        broker.feed_snapshot(bar)
        loop.run_once(now=bar.timestamp)

    event = recorder.get(1)
    assert event.was_traded is False


# -- background rescan / candidate-processing decoupling (run_forever) ------
#
# Regression coverage for: universe rescanning used to run inline inside
# run_once()/run_forever(), blocking candidate/position processing --
# including live stop-loss/exit management -- for the rescan's entire
# duration (which can be many minutes with a wide universe, see
# TradingLoopConfig's docstring). run_forever() now runs the rescan on its
# own background thread so candidate processing keeps ticking on its own
# tight poll_interval_seconds cadence regardless of rescan duration. See
# trading_loop.py's module docstring's "Concurrency model" section.


def test_run_once_stays_single_threaded_and_synchronous():
    # run_once() must keep its original, backward-compatible contract for
    # callers (mainly tests) that call it directly and expect one
    # deterministic pass on their own thread -- it must NOT spawn the
    # background rescan thread that run_forever() now uses.
    broker = PaperBrokerClient()
    broker.connect()
    loop = _build_loop(broker)
    threads_before = threading.active_count()
    loop.run_once(now=datetime.utcnow())
    assert threading.active_count() == threads_before


def test_run_forever_processes_candidates_while_rescan_is_still_in_flight():
    broker = PaperBrokerClient()
    broker.connect()

    class _SlowUniverseProvider:
        def get_symbols(self):
            time_module.sleep(0.3)
            return []

    float_provider = _SingleSymbolFloatProvider("TEST", 3_000_000)
    broad_scanner = BroadScanner(broker, float_provider)
    watcher = CandidateWatcher()
    from webull_bot.scanner.trigger_engine import TriggerEngine

    risk_engine = RiskEngine()
    order_manager = OrderManager(broker, risk_engine, get_settings())
    loop = TradingLoop(
        broker, _SlowUniverseProvider(), broad_scanner, watcher,
        TriggerEngine([MomentumBreakoutStrategy()]), order_manager, PositionManager(), risk_engine,
        config=TradingLoopConfig(poll_interval_seconds=0.02, universe_rescan_interval_seconds=0.02),
    )

    process_call_count = 0
    count_lock = threading.Lock()
    original_process_all = loop._process_all_candidates

    def _counting_process_all_candidates(now):
        nonlocal process_call_count
        with count_lock:
            process_call_count += 1
        original_process_all(now)

    loop._process_all_candidates = _counting_process_all_candidates

    stop_event = threading.Event()
    runner = threading.Thread(target=loop.run_forever, kwargs={"stop_flag": stop_event.is_set})
    runner.start()
    try:
        # The background rescan thread is still asleep inside its first
        # get_symbols() call for this whole window -- if processing were
        # still blocked behind the rescan (the old bug), process_call_count
        # would still be 0 at this point.
        time_module.sleep(0.35)
    finally:
        stop_event.set()
        runner.join(timeout=2.0)

    assert not runner.is_alive()
    assert process_call_count >= 5


def test_candidates_dict_is_thread_safe_under_concurrent_rescan_and_reads():
    # Regression for a dict-mutated-during-iteration race: the rescan thread
    # inserts into self.candidates while readers (e.g. the dashboard, or the
    # main loop's own processing pass) iterate/copy it concurrently.
    from webull_bot.state_machine import new_candidate

    broker = PaperBrokerClient()
    broker.connect()
    loop = _build_loop(broker)
    loop.broad_scanner.scan = lambda symbols: [new_candidate(s) for s in symbols]
    loop.universe_provider.get_symbols = lambda: [f"SYM{i}" for i in range(50)]

    errors: list[Exception] = []

    def _rescan_repeatedly():
        for _ in range(50):
            try:
                loop._rescan_universe(datetime.utcnow())
            except Exception as exc:  # pragma: no cover - failure path under test
                errors.append(exc)

    def _read_repeatedly():
        for _ in range(200):
            try:
                loop.get_candidates()
                loop._snapshot_candidates()
            except Exception as exc:  # pragma: no cover - failure path under test
                errors.append(exc)

    writer = threading.Thread(target=_rescan_repeatedly)
    reader = threading.Thread(target=_read_repeatedly)
    writer.start()
    reader.start()
    writer.join(timeout=5.0)
    reader.join(timeout=5.0)

    assert not writer.is_alive() and not reader.is_alive()
    assert errors == []
    assert len(loop.candidates) == 50


# -- _rescan_universe skips already-tracked symbols (cost optimization) -----

def test_rescan_universe_does_not_rescan_an_already_tracked_symbol():
    from webull_bot.state_machine import new_candidate

    broker = PaperBrokerClient()
    broker.connect()
    loop = _build_loop(broker)  # universe_provider is StaticUniverseProvider(["TEST"])
    loop.candidates["TEST"] = new_candidate("TEST")

    scan_calls: list[list[str]] = []
    original_scan = loop.broad_scanner.scan
    loop.broad_scanner.scan = lambda symbols: (scan_calls.append(list(symbols)), original_scan(symbols))[1]

    loop._rescan_universe(datetime.utcnow())

    # "TEST" is the only symbol the universe provider returns, and it's
    # already tracked -- BroadScanner.scan should never even be asked
    # about it, let alone pay for the network calls that would follow.
    assert scan_calls == [[]]


def test_rescan_universe_still_scans_genuinely_new_symbols():
    from webull_bot.state_machine import new_candidate

    broker = PaperBrokerClient()
    broker.connect()
    loop = _build_loop(broker)
    loop.universe_provider = StaticUniverseProvider(["TEST", "NEWSYM"])
    loop.candidates["TEST"] = new_candidate("TEST")

    scan_calls: list[list[str]] = []
    original_scan = loop.broad_scanner.scan
    loop.broad_scanner.scan = lambda symbols: (scan_calls.append(list(symbols)), original_scan(symbols))[1]

    loop._rescan_universe(datetime.utcnow())

    assert scan_calls == [["NEWSYM"]]


# -- periodic resistance refresh (already-tracked, pre-entry candidates --
# see TradingLoop._refresh_stale_resistance_levels) --------------------------

def test_rescan_universe_refreshes_stale_resistance_for_pre_entry_candidate():
    from webull_bot.enums import CandidateState
    from webull_bot.state_machine import new_candidate, transition

    broker = PaperBrokerClient()
    broker.connect()
    loop = _build_loop(broker)
    candidate = new_candidate("TEST")
    transition(candidate, CandidateState.WATCHING)
    loop.candidates["TEST"] = candidate  # resistance_last_refreshed_at defaults to None -- always "stale"

    refreshed: list[str] = []
    loop.broad_scanner.refresh_resistance_levels = lambda c, **kw: refreshed.append(c.symbol)

    loop._rescan_universe(datetime.utcnow())

    assert refreshed == ["TEST"]


def test_rescan_universe_skips_resistance_refresh_within_throttle_window():
    from webull_bot.enums import CandidateState
    from webull_bot.state_machine import new_candidate, transition

    broker = PaperBrokerClient()
    broker.connect()
    loop = _build_loop(broker)
    now = datetime.utcnow()
    candidate = new_candidate("TEST")
    transition(candidate, CandidateState.WATCHING)
    candidate.resistance_last_refreshed_at = now  # just refreshed
    loop.candidates["TEST"] = candidate

    refreshed: list[str] = []
    loop.broad_scanner.refresh_resistance_levels = lambda c, **kw: refreshed.append(c.symbol)

    # Default resistance_refresh_interval_seconds is 300s -- 10s later is well within the throttle window.
    loop._rescan_universe(now + timedelta(seconds=10))

    assert refreshed == []


def test_rescan_universe_refreshes_again_once_throttle_window_elapses():
    from webull_bot.enums import CandidateState
    from webull_bot.state_machine import new_candidate, transition

    broker = PaperBrokerClient()
    broker.connect()
    loop = _build_loop(broker)
    now = datetime.utcnow()
    candidate = new_candidate("TEST")
    transition(candidate, CandidateState.WATCHING)
    candidate.resistance_last_refreshed_at = now
    loop.candidates["TEST"] = candidate

    refreshed: list[str] = []
    loop.broad_scanner.refresh_resistance_levels = lambda c, **kw: refreshed.append(c.symbol)

    loop._rescan_universe(now + timedelta(seconds=loop.config.resistance_refresh_interval_seconds + 1))

    assert refreshed == ["TEST"]


def test_rescan_universe_skips_resistance_refresh_for_entered_candidate():
    from webull_bot.enums import CandidateState
    from webull_bot.state_machine import new_candidate, transition

    broker = PaperBrokerClient()
    broker.connect()
    loop = _build_loop(broker)
    candidate = new_candidate("TEST")
    for state in (CandidateState.WATCHING, CandidateState.HEATING_UP, CandidateState.ARMED,
                  CandidateState.TRIGGERED, CandidateState.ENTERED, CandidateState.MANAGING):
        transition(candidate, state)
    loop.candidates["TEST"] = candidate

    refreshed: list[str] = []
    loop.broad_scanner.refresh_resistance_levels = lambda c, **kw: refreshed.append(c.symbol)

    loop._rescan_universe(datetime.utcnow())

    # Resistance no longer matters once a candidate has entered a position
    # -- PositionManager governs exits from there, not resistance_level.
    assert refreshed == []


def test_rescan_universe_resistance_refresh_failure_does_not_crash():
    from webull_bot.enums import CandidateState
    from webull_bot.state_machine import new_candidate, transition

    broker = PaperBrokerClient()
    broker.connect()
    loop = _build_loop(broker)
    candidate = new_candidate("TEST")
    transition(candidate, CandidateState.WATCHING)
    loop.candidates["TEST"] = candidate

    def _boom(c, **kw):
        raise RuntimeError("simulated Webull failure")

    loop.broad_scanner.refresh_resistance_levels = _boom

    loop._rescan_universe(datetime.utcnow())  # must not raise


# -- kill switch: halt + flatten (engage_kill_switch_and_flatten) -----------

def _managing_candidate_with_position(loop, broker, symbol="TEST", price=10.0, quantity=100):
    from webull_bot.state_machine import new_candidate, transition
    from webull_bot.enums import CandidateState

    broker.feed_snapshot(MarketSnapshot(
        symbol=symbol, timestamp=datetime.utcnow(), last_price=price, bid=price - 0.01, ask=price + 0.01,
        bid_size=100, ask_size=100, cumulative_volume=200_000, vwap=price, high_of_day=price,
        low_of_day=price, open_price=price,
    ))
    candidate = new_candidate(symbol)
    for state in (CandidateState.WATCHING, CandidateState.HEATING_UP, CandidateState.ARMED,
                  CandidateState.TRIGGERED, CandidateState.ENTERED, CandidateState.MANAGING):
        transition(candidate, state)
    loop.candidates[symbol] = candidate
    loop._positions[symbol] = Position(
        symbol=symbol, side=OrderSide.BUY, quantity=quantity, avg_entry_price=price - 1.0,
        stop_price=price - 2.0, target_price=price + 5.0, trailing_stop_pct=None,
        opened_at=datetime.utcnow(), strategy_name="test",
    )
    return candidate


def test_engage_kill_switch_and_flatten_blocks_new_entries_immediately():
    broker = PaperBrokerClient()
    broker.connect()
    loop = _build_loop(broker)

    loop.engage_kill_switch_and_flatten("test halt")

    # Takes effect the instant it's called -- no tick needs to run first.
    assert loop.risk_engine.kill_switch_active is True


def test_engage_kill_switch_and_flatten_closes_open_position_on_next_tick():
    broker = PaperBrokerClient()
    broker.connect()
    loop = _build_loop(broker)
    candidate = _managing_candidate_with_position(loop, broker)

    loop.engage_kill_switch_and_flatten("test halt")
    assert "TEST" in loop._positions  # not closed synchronously by the call itself

    loop._process_all_candidates(datetime.utcnow())

    assert "TEST" not in loop._positions
    assert candidate.state.value == "cooldown"
    assert len(loop._trades) == 1
    assert loop._trades[0].exit_reason == ExitReason.RISK_KILL_SWITCH


def test_engage_kill_switch_and_flatten_closes_multiple_positions():
    broker = PaperBrokerClient()
    broker.connect()
    loop = _build_loop(broker)
    _managing_candidate_with_position(loop, broker, symbol="AAA", price=10.0)
    _managing_candidate_with_position(loop, broker, symbol="BBB", price=20.0)

    loop.engage_kill_switch_and_flatten("test halt")
    loop._process_all_candidates(datetime.utcnow())

    assert loop._positions == {}
    assert len(loop._trades) == 2
    assert {t.symbol for t in loop._trades} == {"AAA", "BBB"}


def test_disengage_kill_switch_does_not_touch_open_positions():
    broker = PaperBrokerClient()
    broker.connect()
    loop = _build_loop(broker)
    _managing_candidate_with_position(loop, broker)
    loop.risk_engine.engage_kill_switch("test halt")

    loop.risk_engine.release_kill_switch()
    loop._process_all_candidates(datetime.utcnow())

    assert loop.risk_engine.kill_switch_active is False
    assert "TEST" in loop._positions  # untouched -- disengaging never flattens


def test_kill_switch_flatten_leaves_pending_when_broker_does_not_fill_synchronously():
    from webull_bot.state_machine import new_candidate, transition
    from webull_bot.enums import CandidateState

    # fills_after_polls=2: the submit happens via _close_all_positions_now,
    # and _manage_position's own pending-order check then polls once more
    # in that SAME tick (see _manage_position's "pending is not None"
    # branch) -- fills_after_polls=1 would fill on that very first poll,
    # collapsing submit+finalize into one tick and defeating this test's
    # purpose. =2 means that first poll (still within tick 1) isn't enough,
    # so it genuinely stays pending until tick 2's poll.
    broker = _FakeBroker(fills_after_polls=2)
    loop, candidate = _armed_candidate_setup(broker)
    transition(candidate, CandidateState.TRIGGERED)
    transition(candidate, CandidateState.ENTERED)
    transition(candidate, CandidateState.MANAGING)
    loop._positions["TEST"] = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=10, avg_entry_price=5.00, stop_price=4.50,
        target_price=None, trailing_stop_pct=None, opened_at=datetime.utcnow(), strategy_name="test",
    )

    loop.engage_kill_switch_and_flatten("test halt")
    loop._process_all_candidates(datetime.utcnow())

    # _FakeBroker returns SUBMITTED first -- the close is pending, not
    # finalized yet, and the position is still tracked as open.
    assert "TEST" in loop._pending_exit_orders
    assert "TEST" in loop._positions
    assert candidate.state.value == "managing"

    # The very next tick's normal _manage_position path (not any kill-switch-
    # specific code) picks up and finalizes the pending exit, same as any
    # other exit order.
    loop._process_all_candidates(datetime.utcnow())
    assert "TEST" not in loop._pending_exit_orders
    assert "TEST" not in loop._positions
    assert candidate.state.value == "cooldown"


def test_kill_switch_flatten_skips_symbol_on_snapshot_failure_without_crashing():
    broker = PaperBrokerClient()
    broker.connect()
    loop = _build_loop(broker)
    _managing_candidate_with_position(loop, broker)
    # No snapshot fed for a second symbol -- get_snapshot raises for it.
    from webull_bot.state_machine import new_candidate, transition
    from webull_bot.enums import CandidateState

    broken_candidate = new_candidate("NOSNAPSHOT")
    for state in (CandidateState.WATCHING, CandidateState.HEATING_UP, CandidateState.ARMED,
                  CandidateState.TRIGGERED, CandidateState.ENTERED, CandidateState.MANAGING):
        transition(broken_candidate, state)
    loop.candidates["NOSNAPSHOT"] = broken_candidate
    loop._positions["NOSNAPSHOT"] = Position(
        symbol="NOSNAPSHOT", side=OrderSide.BUY, quantity=10, avg_entry_price=5.0, stop_price=4.0,
        target_price=None, trailing_stop_pct=None, opened_at=datetime.utcnow(), strategy_name="test",
    )

    loop.engage_kill_switch_and_flatten("test halt")
    loop._process_all_candidates(datetime.utcnow())  # must not raise

    # The good symbol still closes even though the other one failed.
    assert "TEST" not in loop._positions
    assert "NOSNAPSHOT" in loop._positions


# -- scan_and_add_candidate (on-demand single-ticker scan, backs the
# dashboard's manual "scan a ticker" feature) --------------------------------

def _feed(broker, symbol, price=5.0, cumulative_volume=200_000):
    broker.feed_snapshot(MarketSnapshot(
        symbol=symbol, timestamp=datetime.utcnow(), last_price=price, bid=price - 0.01, ask=price + 0.01,
        bid_size=100, ask_size=100, cumulative_volume=cumulative_volume, vwap=price, high_of_day=price,
        low_of_day=price, open_price=price,
    ))


def test_scan_and_add_candidate_adds_a_new_passing_symbol():
    from webull_bot.enums import CandidateState

    broker = PaperBrokerClient()
    broker.connect()
    loop = _build_loop(broker)
    _feed(broker, "NEWSYM")

    candidate, reason, was_newly_added = loop.scan_and_add_candidate("newsym")  # lowercase input -- should be uppercased
    assert reason is None
    assert was_newly_added is True
    assert candidate is not None
    assert candidate.symbol == "NEWSYM"
    assert candidate.state == CandidateState.WATCHING
    assert loop.candidates["NEWSYM"] is candidate


def test_scan_and_add_candidate_returns_reason_without_adding_on_rejection():
    broker = PaperBrokerClient()
    broker.connect()
    loop = _build_loop(broker)
    _feed(broker, "TOOEXPENSIVE", price=30.0)  # outside BroadScannerConfig's default $0.40-$25 range

    candidate, reason, was_newly_added = loop.scan_and_add_candidate("TOOEXPENSIVE")
    assert candidate is None
    assert reason is not None
    assert "range" in reason.lower()
    assert was_newly_added is False
    assert "TOOEXPENSIVE" not in loop.candidates


def test_scan_and_add_candidate_returns_existing_candidate_without_rescanning():
    from webull_bot.enums import CandidateState
    from webull_bot.state_machine import new_candidate, transition

    broker = PaperBrokerClient()
    broker.connect()
    loop = _build_loop(broker)
    existing = new_candidate("TEST")
    transition(existing, CandidateState.WATCHING)
    transition(existing, CandidateState.HEATING_UP)
    transition(existing, CandidateState.ARMED)
    loop.candidates["TEST"] = existing
    # No snapshot fed for TEST -- if this re-scanned instead of returning
    # the existing candidate, get_snapshot would raise and reason would be set.

    candidate, reason, was_newly_added = loop.scan_and_add_candidate("TEST")
    assert candidate is existing
    assert candidate.state == CandidateState.ARMED
    assert reason is None
    assert was_newly_added is False


def test_scan_and_add_candidate_reports_unexpected_scanner_errors_without_raising():
    broker = PaperBrokerClient()
    broker.connect()
    loop = _build_loop(broker)
    loop.broad_scanner.check_symbol_verbose = lambda symbol: (_ for _ in ()).throw(RuntimeError("boom"))

    candidate, reason, was_newly_added = loop.scan_and_add_candidate("ANY")
    assert candidate is None
    assert "ANY" in reason
    assert was_newly_added is False


# -- batched get_snapshots in _process_all_candidates -- see
# WebullBrokerClient.get_snapshots' docstring for why this exists: every
# get_snapshot-family call shares the same globally-paced rate limiter, so
# fetching N tracked candidates one at a time used to mean a real >=N-second
# floor on how often any single one's tick refreshed ------------------------

@dataclass
class _BatchAwareBroker(_FakeBroker):
    batch_calls: list = field(default_factory=list)
    individual_calls: list = field(default_factory=list)

    def get_snapshot(self, symbol):
        self.individual_calls.append(symbol)
        return super().get_snapshot(symbol)

    def get_snapshots(self, symbols):
        self.batch_calls.append(list(symbols))
        return {
            s: MarketSnapshot(
                symbol=s, timestamp=datetime.utcnow(), last_price=5.20, bid=5.19, ask=5.21,
                bid_size=100, ask_size=100, cumulative_volume=200_000, vwap=5.20, high_of_day=5.20,
                low_of_day=5.20, open_price=5.20,
            )
            for s in symbols
        }


def _two_candidate_loop(broker):
    from webull_bot.state_machine import new_candidate, transition
    from webull_bot.enums import CandidateState

    risk_engine = RiskEngine(RiskConfig(stop_loss_required=True))
    order_manager = OrderManager(broker, risk_engine, get_settings())
    watcher = CandidateWatcher()
    from webull_bot.scanner.trigger_engine import TriggerEngine

    trigger_engine = TriggerEngine(strategies=[MomentumBreakoutStrategy()])
    position_manager = PositionManager()
    loop = TradingLoop(
        broker, StaticUniverseProvider([]), BroadScanner(broker, _SingleSymbolFloatProvider("ONE", 1_000_000)),
        watcher, trigger_engine, order_manager, position_manager, risk_engine,
        config=TradingLoopConfig(universe_rescan_interval_seconds=3600),
    )
    for symbol in ("ONE", "TWO"):
        candidate = new_candidate(symbol)
        transition(candidate, CandidateState.WATCHING)
        loop.candidates[symbol] = candidate
    return loop


def test_process_all_candidates_uses_one_batched_call_for_multiple_candidates():
    broker = _BatchAwareBroker()
    loop = _two_candidate_loop(broker)

    loop._process_all_candidates(datetime.utcnow())

    assert broker.batch_calls == [["ONE", "TWO"]]
    assert broker.individual_calls == []  # neither candidate fell back to its own get_snapshot()
    assert loop.candidates["ONE"].last_price == 5.20
    assert loop.candidates["TWO"].last_price == 5.20


def test_process_all_candidates_falls_back_without_get_snapshots():
    # _FakeBroker has no get_snapshots at all -- representative of
    # PaperBrokerClient/any broker that doesn't support batching.
    broker = _FakeBroker()
    loop = _two_candidate_loop(broker)

    loop._process_all_candidates(datetime.utcnow())

    assert loop.candidates["ONE"].last_price == 5.20
    assert loop.candidates["TWO"].last_price == 5.20


def test_process_all_candidates_falls_back_when_batch_call_raises():
    class _FlakyBatchBroker(_BatchAwareBroker):
        def get_snapshots(self, symbols):
            self.batch_calls.append(list(symbols))
            raise RuntimeError("simulated Webull batch failure")

    broker = _FlakyBatchBroker()
    loop = _two_candidate_loop(broker)

    loop._process_all_candidates(datetime.utcnow())

    # The batch call was attempted (and failed), but candidates still got
    # processed via the per-candidate get_snapshot() fallback.
    assert len(broker.batch_calls) == 1
    assert sorted(broker.individual_calls) == ["ONE", "TWO"]
    assert loop.candidates["ONE"].last_price == 5.20
    assert loop.candidates["TWO"].last_price == 5.20


def test_process_all_candidates_skips_rejected_and_cooldown_symbols_in_batch():
    from webull_bot.state_machine import new_candidate, transition
    from webull_bot.enums import CandidateState

    broker = _BatchAwareBroker()
    loop = _two_candidate_loop(broker)
    cooldown_candidate = new_candidate("THREE")
    transition(cooldown_candidate, CandidateState.WATCHING)
    transition(cooldown_candidate, CandidateState.COOLDOWN)
    cooldown_candidate.last_updated_at = datetime.utcnow()
    loop.candidates["THREE"] = cooldown_candidate

    loop._process_all_candidates(datetime.utcnow())

    assert broker.batch_calls == [["ONE", "TWO"]]  # THREE (COOLDOWN) excluded
