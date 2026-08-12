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
from webull_bot.enums import CandidateState, ExitReason, OrderSide, OrderStatus, OrderType
from webull_bot.execution.order_manager import OrderManager
from webull_bot.interfaces.broker import BrokerClient
from webull_bot.interfaces.float_provider import FloatDataProvider
from webull_bot.models import Fill, FloatData, MarketSnapshot, Order, Position
from webull_bot.position.position_manager import PositionManagementConfig, PositionManager
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


# 15:00 UTC = 11:00am US/Eastern in August (EDT, UTC-4) on a real Monday --
# fixed, deterministic, and safely inside core trading hours. Used in place
# of datetime.utcnow() wherever a test feeds `now` into candidate/position
# processing (run_once, _process_all_candidates): RiskEngine.evaluate's
# core-hours gate and TradingLoop's end-of-core-hours auto-flatten both key
# off that `now`, so leaving it as the real wall clock would make many of
# this file's assertions about entries/positions depend on what time of day
# (and day of week) the suite happens to run.
_IN_HOURS_NOW = datetime(2026, 8, 10, 15, 0, 0)
# 09:00 UTC = 5:00am ET on the same day -- pre-market, outside core hours.
_PRE_MARKET_NOW = datetime(2026, 8, 10, 9, 0, 0)


def _build_bars() -> list[MarketSnapshot]:
    # 14:31 UTC = 9:31am US/Eastern in January (EST, UTC-5) -- a real,
    # deterministic weekday (Monday) time within core trading hours, needed
    # now that RiskEngine.evaluate's core-hours gate is threaded through
    # from this bar's own timestamp (via TradingLoop's `now`) rather than
    # silently falling back to the real wall clock at test-run time.
    t0 = datetime(2026, 1, 5, 14, 31, 0)
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
    # max_position_size_pct pinned below the 100%-of-buying-power default so
    # a sized order's notional (computed off the signal's reference price)
    # still clears PaperBrokerClient's cash check once the actual fill price
    # is nudged up by simulated slippage -- sizing at exactly 100% of buying
    # power leaves zero room for that and the order gets rejected for
    # insufficient funds before ever filling.
    risk_engine = RiskEngine(RiskConfig(stop_loss_required=True, max_position_size_pct=90.0))
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


@dataclass
class _RestingBroker(_FakeBroker):
    """Adds place_oco_bracket -- the capability OrderManager/TradingLoop
    detect via getattr (see order_manager.py's _broker_supports_resting_orders)
    -- so this stands in for WebullBrokerClient's broker-side bracket
    feature without any real network/SDK access. Resting orders (both a
    lone stop from place_resting_stop and either leg of an OCO bracket from
    place_resting_bracket) are tracked separately in _resting_orders rather
    than through _FakeBroker's own _orders/_poll_counts auto-fill-after-N-
    polls machinery, so a test can flip exactly one leg to FILLED at an
    exact moment instead of every order auto-filling after a fixed number
    of polls."""
    _resting_orders: dict = field(default_factory=dict)
    _brackets: list = field(default_factory=list)
    _lone_stops: list = field(default_factory=list)
    _cancelled: list = field(default_factory=list)
    list_open_orders_calls: int = 0
    get_order_status_calls: int = 0

    def place_oco_bracket(self, stop_order, target_order):
        stop_order.broker_order_id = f"stop-{len(self._brackets)}"
        stop_order.status = OrderStatus.SUBMITTED
        target_order.broker_order_id = f"target-{len(self._brackets)}"
        target_order.status = OrderStatus.SUBMITTED
        self._resting_orders[stop_order.broker_order_id] = stop_order
        self._resting_orders[target_order.broker_order_id] = target_order
        self._brackets.append((stop_order, target_order))
        return stop_order, target_order

    def place_order(self, order):
        if order.order_type in (OrderType.STOP, OrderType.TRAILING_STOP):
            order.broker_order_id = order.client_order_id or f"lone-stop-{len(self._lone_stops)}"
            order.status = OrderStatus.SUBMITTED
            self._resting_orders[order.broker_order_id] = order
            self._lone_stops.append(order)
            return order
        return super().place_order(order)

    def get_order_status(self, broker_order_id):
        self.get_order_status_calls += 1
        if broker_order_id in self._resting_orders:
            return self._resting_orders[broker_order_id]
        return super().get_order_status(broker_order_id)

    def cancel_order(self, broker_order_id):
        self._cancelled.append(broker_order_id)
        order = self._resting_orders.get(broker_order_id)
        if order is not None:
            order.status = OrderStatus.CANCELED

    def list_open_orders(self):
        # Mirrors get_order_open's real semantics: only currently-resting
        # (SUBMITTED) orders come back -- a leg a test flipped to FILLED/
        # CANCELED via _resting_orders directly must NOT still show up
        # here, or _poll_broker_bracket's batched-skip check would wrongly
        # treat it as still resting and never notice the fill.
        self.list_open_orders_calls += 1
        return [o for o in self._resting_orders.values() if o.status == OrderStatus.SUBMITTED]


@dataclass
class _StreamingBroker(_FakeBroker):
    """Adds a working subscribe_quotes -- the capability TradingLoop
    detects via a try/except around the call itself (subscribe_quotes is
    a required BrokerClient ABC method, unlike get_snapshots/
    place_oco_bracket/list_open_orders, so there's no getattr presence
    check to use instead -- see _ensure_streaming_subscribed's
    docstring). Captures the on_update callback so a test can simulate a
    real streamed message arriving via push_stream_snapshot, exactly the
    way WebullBrokerClient._on_quotes_message would call it from the
    MQTT client's own background thread in production."""
    subscribe_calls: list = field(default_factory=list)
    individual_calls: list = field(default_factory=list)
    fail_subscribe_times: int = 0
    _stream_callback: object = None

    def subscribe_quotes(self, symbols, on_update):
        self.subscribe_calls.append(list(symbols))
        if self.fail_subscribe_times > 0:
            self.fail_subscribe_times -= 1
            raise RuntimeError("simulated transient streaming failure")
        self._stream_callback = on_update

    def push_stream_snapshot(self, snapshot):
        assert self._stream_callback is not None, "subscribe_quotes was never called"
        self._stream_callback(snapshot)

    def get_snapshot(self, symbol):
        self.individual_calls.append(symbol)
        return super().get_snapshot(symbol)


def _watching_candidate_setup(broker):
    """Same wiring as _armed_candidate_setup, but leaves the candidate in
    WATCHING -- for tests covering the pre-entry (WATCHING/HEATING_UP/
    ARMED) streaming path rather than the exit-management one."""
    from webull_bot.state_machine import new_candidate, transition
    from webull_bot.enums import CandidateState

    candidate = new_candidate("TEST")
    candidate.resistance_level = 5.10
    transition(candidate, CandidateState.WATCHING)

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

    snapshot = _snapshot(_IN_HOURS_NOW, 5.20, 5.20, 600_000, 5.19, 5.21, 5.15)
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


def test_submit_entry_uses_locally_tracked_positions_for_the_total_risk_gate():
    # Real bug fixed 2026-08-11 (see order_manager.py's submit_signal
    # docstring): the max_total_risk_pct gate used to be fed
    # broker.get_positions(), whose returned Positions always hard-code
    # stop_price=None (no such field in a broker's raw account-positions
    # response) -- so it silently saw zero assumed risk from any existing
    # position and could never reject a new entry on that basis, no matter
    # how much risk was already on. Proves the fix end-to-end through the
    # real _submit_entry pipeline: a position this process is already
    # tracking locally (loop._positions, with a real stop_price) now
    # correctly blocks a new entry once its assumed risk breaches the
    # ceiling -- using _FakeBroker's default empty get_positions() so a
    # regression back to reading the broker's own (always risk-blind)
    # position list would show up as this test's new entry wrongly being
    # approved instead of rejected.
    from webull_bot.scanner.trigger_engine import TriggerEngine

    broker = _FakeBroker()  # get_positions() returns [] by default -- irrelevant now for entry sizing
    risk_engine = RiskEngine(RiskConfig(stop_loss_required=True, max_total_risk_pct=10.0))  # ceiling: $2,500 (25,000 equity)
    order_manager = OrderManager(broker, risk_engine, get_settings())
    loop = TradingLoop(
        broker, StaticUniverseProvider([]), BroadScanner(broker, _SingleSymbolFloatProvider("TEST", 1_000_000)),
        CandidateWatcher(), TriggerEngine(strategies=[MomentumBreakoutStrategy()]),
        order_manager, PositionManager(), risk_engine,
        config=TradingLoopConfig(universe_rescan_interval_seconds=3600),
    )
    from webull_bot.state_machine import new_candidate, transition
    candidate = new_candidate("TEST")
    transition(candidate, CandidateState.WATCHING)
    transition(candidate, CandidateState.HEATING_UP)
    transition(candidate, CandidateState.ARMED)
    loop.candidates["TEST"] = candidate

    # A position this process already has open (a different symbol), with
    # $4,000 of assumed risk ($4.00/share * 1,000 shares) -- well over the
    # $2,500 ceiling.
    loop._positions["EFGH"] = Position(
        symbol="EFGH", side=OrderSide.BUY, quantity=1_000, avg_entry_price=5.0,
        stop_price=1.0, target_price=None, trailing_stop_pct=None,
        opened_at=datetime.utcnow(), strategy_name="test",
    )

    snapshot = _snapshot(_IN_HOURS_NOW, 5.20, 5.20, 600_000, 5.19, 5.21, 5.15)
    signal = _trigger_and_build_signal(candidate, snapshot)

    loop._submit_entry(candidate, signal, snapshot, snapshot.timestamp)

    assert candidate.symbol not in loop._pending_entry_orders
    assert candidate.state.value == "armed"  # reverted -- OrderRejected, never placed
    assert "TEST" not in loop._positions


def test_confirm_entry_filled_still_tracks_position_when_broker_get_positions_raises():
    # Real production incident: WebullBrokerClient.get_positions() raised
    # (a real, populated get_account_position() response hit a field-name
    # mismatch never verified against a live row -- see that method's
    # docstring) right as an entry order filled. The old code only caught
    # StopIteration ("no matching position") around this lookup, so any
    # other exception propagated out of _confirm_entry_filled *before*
    # self._positions[symbol] was ever assigned -- the fill happened at the
    # broker, but the bot never recorded it: no stop-loss management, not
    # shown as an open position anywhere, buying power silently consumed.
    # This proves the fallback now covers ANY exception, not just "no match".
    class _BrokerPositionsBlowUp(_FakeBroker):
        # submit_signal's entry path no longer calls broker.get_positions()
        # at all (2026-08-11 fix -- it uses the caller's own
        # locally-tracked open_positions instead, see order_manager.py's
        # docstring), so the only remaining call here is
        # _confirm_entry_filled's own post-fill reconciliation lookup --
        # simulate the real incident on that one directly.
        def get_positions(self):
            raise KeyError("simulated _position_from_dict field-name mismatch")

    broker = _BrokerPositionsBlowUp(fills_after_polls=1)
    loop, candidate = _armed_candidate_setup(broker)
    from webull_bot.models import Signal
    from webull_bot.enums import SignalAction, CandidateState
    from webull_bot.state_machine import transition

    snapshot = _snapshot(_IN_HOURS_NOW, 5.20, 5.20, 600_000, 5.19, 5.21, 5.15)
    transition(candidate, CandidateState.TRIGGERED)
    signal = Signal(
        symbol="TEST", action=SignalAction.ENTER_LONG, generated_at=snapshot.timestamp,
        strategy_name="test", strategy_version="v1", reference_price=5.20, suggested_stop=5.00,
    )

    loop._submit_entry(candidate, signal, snapshot, snapshot.timestamp)
    loop._poll_pending_entry(candidate, snapshot.timestamp)  # fills_after_polls=1: fills on this poll

    # Despite get_positions() raising, the fill must still be tracked
    # locally (falling back to the signal's reference price / order
    # quantity) and the candidate must reach MANAGING, not be left
    # stranded in TRIGGERED or silently reverted to ARMED.
    assert candidate.state.value == "managing"
    assert "TEST" in loop._positions
    assert loop._positions["TEST"].avg_entry_price == 5.20  # fallback: signal.reference_price


def _trigger_and_build_signal(candidate, snapshot):
    from webull_bot.enums import CandidateState, SignalAction
    from webull_bot.models import Signal
    from webull_bot.state_machine import transition

    transition(candidate, CandidateState.TRIGGERED)
    return Signal(
        symbol=candidate.symbol, action=SignalAction.ENTER_LONG, generated_at=snapshot.timestamp,
        strategy_name="test", strategy_version="v1", reference_price=5.20, suggested_stop=5.00,
    )


# -- RiskEngine.record_entry_order_failed rollback -- see that method's
# docstring for the real production bug this fixes: a broker-rejected order
# (no position ever opened) was permanently consuming a real trade's slot,
# eventually exhausting max_trades_per_ticker_per_day with zero actual trades

def test_submit_entry_rolls_back_ticker_count_on_immediate_broker_rejection():
    class _ImmediatelyRejectingBroker(_FakeBroker):
        def place_order(self, order):
            order.status = OrderStatus.REJECTED
            order.broker_order_id = order.client_order_id
            return order

    broker = _ImmediatelyRejectingBroker()
    loop, candidate = _armed_candidate_setup(broker)
    snapshot = _snapshot(_IN_HOURS_NOW, 5.20, 5.20, 600_000, 5.19, 5.21, 5.15)
    signal = _trigger_and_build_signal(candidate, snapshot)

    loop._submit_entry(candidate, signal, snapshot, snapshot.timestamp)

    assert candidate.symbol not in loop._pending_entry_orders
    assert candidate.state.value == "armed"
    # The approval's optimistic increment must be rolled back -- no
    # position ever opened, so this must not count against the ticker.
    assert loop.risk_engine._daily.trades_per_ticker.get("TEST", 0) == 0


def test_poll_pending_entry_rolls_back_ticker_count_when_later_rejected():
    class _LaterRejectingBroker(_FakeBroker):
        def get_order_status(self, broker_order_id):
            order = self._orders[broker_order_id]
            order.status = OrderStatus.REJECTED
            return order

    broker = _LaterRejectingBroker()
    loop, candidate = _armed_candidate_setup(broker)
    snapshot = _snapshot(_IN_HOURS_NOW, 5.20, 5.20, 600_000, 5.19, 5.21, 5.15)
    signal = _trigger_and_build_signal(candidate, snapshot)

    loop._submit_entry(candidate, signal, snapshot, snapshot.timestamp)
    assert candidate.symbol in loop._pending_entry_orders  # SUBMITTED initially
    assert loop.risk_engine._daily.trades_per_ticker.get("TEST", 0) == 1  # approved so far

    loop._poll_pending_entry(candidate, snapshot.timestamp)

    assert candidate.symbol not in loop._pending_entry_orders
    assert candidate.state.value == "armed"
    assert loop.risk_engine._daily.trades_per_ticker.get("TEST", 0) == 0


def test_repeated_broker_rejections_never_exhaust_the_ticker_budget():
    # RiskConfig's default max_trades_per_ticker_per_day is 2 -- before this
    # fix, two consecutive broker-level rejections alone (zero real
    # positions ever opened) would have exhausted it.
    class _AlwaysRejectingBroker(_FakeBroker):
        def place_order(self, order):
            order.status = OrderStatus.REJECTED
            order.broker_order_id = order.client_order_id
            return order

    broker = _AlwaysRejectingBroker()
    loop, candidate = _armed_candidate_setup(broker)
    snapshot = _snapshot(_IN_HOURS_NOW, 5.20, 5.20, 600_000, 5.19, 5.21, 5.15)

    for _ in range(2):
        signal = _trigger_and_build_signal(candidate, snapshot)
        loop._submit_entry(candidate, signal, snapshot, snapshot.timestamp)
        assert candidate.state.value == "armed"

    # A third attempt must still be risk-approved -- the two failed
    # attempts above never actually consumed the ticker's real budget.
    signal = _trigger_and_build_signal(candidate, snapshot)
    loop._submit_entry(candidate, signal, snapshot, snapshot.timestamp)

    from webull_bot.enums import RiskEventType

    assert not any(
        e.event_type == RiskEventType.MAX_TRADES_PER_TICKER_HIT.value for e in loop.risk_engine.events
    )


def test_submit_entry_reverts_to_armed_on_unexpected_broker_exception():
    # Confirmed real production case: order_manager.submit_signal raising
    # anything other than OrderRejected (a real broker/network error, not a
    # risk-engine rejection) used to leave the candidate stuck in TRIGGERED
    # with no order ever recorded, relying on _poll_pending_entry's
    # "shouldn't happen" fallback to eventually notice and revert it --
    # which could take a long time. This must now be caught and reverted
    # immediately instead.
    class _BrokenBroker(_FakeBroker):
        def place_order(self, order):
            raise RuntimeError("simulated unexpected broker failure")

    broker = _BrokenBroker()
    loop, candidate = _armed_candidate_setup(broker)
    snapshot = _snapshot(_IN_HOURS_NOW, 5.20, 5.20, 600_000, 5.19, 5.21, 5.15)
    signal = _trigger_and_build_signal(candidate, snapshot)

    loop._submit_entry(candidate, signal, snapshot, snapshot.timestamp)

    # Reverted immediately -- not left stranded in TRIGGERED.
    assert candidate.state.value == "armed"
    assert candidate.symbol not in loop._pending_entry_orders
    # The failed attempt must not have consumed a real trade slot either.
    assert loop.risk_engine._daily.trades_per_ticker.get("TEST", 0) == 0


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


def test_manage_position_survives_an_unexpected_broker_exception_on_stop_loss():
    # Real production incident: a position sat well past its stop_price
    # with the stop never firing. _manage_position's exit-submission catch
    # only handled OrderRejected -- any other exception from
    # broker.place_order (via order_manager.submit_signal) propagated all
    # the way up to _process_all_candidates' generic per-candidate
    # catch-all, which kept the loop alive but gave no clear signal about
    # WHERE it failed. This proves the position is neither lost nor left
    # in a broken state, and that check_exit gets a fair try again next
    # tick once the transient failure clears.
    class _BrokenSellBroker(_FakeBroker):
        def place_order(self, order):
            if order.side == OrderSide.SELL:
                raise RuntimeError("simulated unexpected broker failure on exit")
            return super().place_order(order)

    from webull_bot.state_machine import transition
    from webull_bot.enums import CandidateState

    broker = _BrokenSellBroker()
    loop, candidate = _armed_candidate_setup(broker)
    transition(candidate, CandidateState.TRIGGERED)
    transition(candidate, CandidateState.ENTERED)
    transition(candidate, CandidateState.MANAGING)
    loop._positions["TEST"] = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=10, avg_entry_price=5.00, stop_price=4.50,
        target_price=None, trailing_stop_pct=None, opened_at=datetime.utcnow(), strategy_name="test",
    )

    snapshot = _snapshot(datetime.utcnow(), 4.00, 4.00, 600_000, 3.99, 4.01, 4.50)  # well past the 4.50 stop
    loop._manage_position(candidate, snapshot, snapshot.timestamp)  # must not raise

    # Position/candidate survive untouched, ready for check_exit to fire
    # again on the very next tick -- not lost, not stuck in a pending state
    # that never resolves.
    assert "TEST" in loop._positions
    assert candidate.symbol not in loop._pending_exit_orders
    assert candidate.state.value == "managing"
    assert loop._positions["TEST"].quantity == 10


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
    loop.run_once(now=_IN_HOURS_NOW)
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
    position = Position(
        symbol=symbol, side=OrderSide.BUY, quantity=quantity, avg_entry_price=price - 1.0,
        stop_price=price - 2.0, target_price=price + 5.0, trailing_stop_pct=None,
        opened_at=datetime.utcnow(), strategy_name="test",
    )
    loop._positions[symbol] = position
    # Also register with the broker itself (PaperBrokerClient's own
    # position store), not just the loop's local tracking -- needed since
    # reconcile_positions_from_broker (see _process_all_candidates) now
    # actively drops any locally-tracked position the broker doesn't also
    # report, and would otherwise treat this test-injected position as
    # having been closed externally the moment the loop ticks.
    if hasattr(broker, "_state"):
        broker._state.positions[symbol] = position
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

    loop._process_all_candidates(_IN_HOURS_NOW)

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
    loop._process_all_candidates(_IN_HOURS_NOW)

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
    loop._process_all_candidates(_IN_HOURS_NOW)

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
    loop._process_all_candidates(_IN_HOURS_NOW)

    # _FakeBroker returns SUBMITTED first -- the close is pending, not
    # finalized yet, and the position is still tracked as open.
    assert "TEST" in loop._pending_exit_orders
    assert "TEST" in loop._positions
    assert candidate.state.value == "managing"

    # The very next tick's normal _manage_position path (not any kill-switch-
    # specific code) picks up and finalizes the pending exit, same as any
    # other exit order.
    loop._process_all_candidates(_IN_HOURS_NOW)
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
    nosnapshot_position = Position(
        symbol="NOSNAPSHOT", side=OrderSide.BUY, quantity=10, avg_entry_price=5.0, stop_price=4.0,
        target_price=None, trailing_stop_pct=None, opened_at=datetime.utcnow(), strategy_name="test",
    )
    loop._positions["NOSNAPSHOT"] = nosnapshot_position
    # Also register with the broker's own position store -- see
    # _managing_candidate_with_position's comment on why this matters now
    # that reconcile_positions_from_broker actively drops anything the
    # broker doesn't also report.
    broker._state.positions["NOSNAPSHOT"] = nosnapshot_position

    loop.engage_kill_switch_and_flatten("test halt")
    loop._process_all_candidates(_IN_HOURS_NOW)  # must not raise

    # The good symbol still closes even though the other one failed.
    assert "TEST" not in loop._positions
    assert "NOSNAPSHOT" in loop._positions


def test_kill_switch_flatten_survives_an_unexpected_broker_exception_on_one_symbol():
    # Same class of production incident as _manage_position's own fix
    # (test_manage_position_survives_an_unexpected_broker_exception_on_stop_loss)
    # applied to the flatten-everything path instead: this used to only
    # catch OrderRejected around the exit submission, so any other
    # exception from broker.place_order would propagate out of
    # _close_all_positions_now entirely, aborting the flatten for every
    # OTHER symbol too -- exactly the "one bad symbol shouldn't leave
    # everything else uncautiously open" contract this method's own
    # docstring already promises for a get_snapshot failure.
    broker = PaperBrokerClient()
    broker.connect()
    loop = _build_loop(broker)
    _managing_candidate_with_position(loop, broker, symbol="AAA", price=10.0)
    _managing_candidate_with_position(loop, broker, symbol="BBB", price=20.0)

    original_submit = loop.order_manager.submit_signal

    def _flaky_submit(signal, **kwargs):
        if signal.symbol == "AAA":
            raise RuntimeError("simulated unexpected broker failure")
        return original_submit(signal, **kwargs)

    loop.order_manager.submit_signal = _flaky_submit

    loop.engage_kill_switch_and_flatten("test halt")
    loop._process_all_candidates(_IN_HOURS_NOW)  # must not raise

    assert "AAA" in loop._positions  # left open, ready to retry next tick
    assert "BBB" not in loop._positions  # unaffected by AAA's failure


def test_kill_switch_flatten_retries_and_eventually_closes_a_position_that_failed_once():
    # Regression test for a real production incident (2026-08-11): the
    # kill switch's flatten used to be a one-shot flag consumed the
    # instant a tick saw it -- a single failed close attempt on any
    # symbol, for any reason, permanently abandoned the flatten for that
    # symbol, with no further retry ever (unlike the end-of-day
    # auto-flatten, which already re-checked its trigger every tick).
    # This is what made the kill switch appear to silently "do nothing"
    # whenever the very first attempt hit a transient failure. Fixed by
    # driving the retry off risk_engine.kill_switch_active every tick,
    # exactly like the auto-flatten -- this proves a symbol that failed
    # once genuinely gets retried and actually closes, not just that the
    # loop doesn't crash.
    broker = PaperBrokerClient()
    broker.connect()
    loop = _build_loop(broker)
    _managing_candidate_with_position(loop, broker, symbol="AAA", price=10.0)

    original_submit = loop.order_manager.submit_signal
    calls = {"count": 0}

    def _fails_once_then_succeeds(signal, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("simulated transient broker failure")
        return original_submit(signal, **kwargs)

    loop.order_manager.submit_signal = _fails_once_then_succeeds

    loop.engage_kill_switch_and_flatten("test halt")
    loop._process_all_candidates(_IN_HOURS_NOW)  # tick 1: fails, must not raise
    assert "AAA" in loop._positions

    loop._process_all_candidates(_IN_HOURS_NOW)  # tick 2: retried, succeeds
    assert "AAA" not in loop._positions
    assert len(loop._trades) == 1
    assert loop._trades[0].exit_reason == ExitReason.RISK_KILL_SWITCH


def test_kill_switch_flatten_does_not_double_submit_while_a_close_is_still_pending():
    # Regression test for a risk introduced by making the flatten retry
    # every tick (see the test above): without a guard, a symbol whose
    # close order is still pending (a real broker returns SUBMITTED, not
    # an instant FILLED) would get a SECOND market exit order submitted
    # against it on the very next tick, before the first one even
    # resolved -- a real over-sell risk against a live broker.
    broker = _FakeBroker(fills_after_polls=5)  # stays pending across both ticks below
    loop, candidate = _armed_candidate_setup(broker)
    from webull_bot.state_machine import transition

    transition(candidate, CandidateState.TRIGGERED)
    transition(candidate, CandidateState.ENTERED)
    transition(candidate, CandidateState.MANAGING)
    loop._positions["TEST"] = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=10, avg_entry_price=5.00, stop_price=4.50,
        target_price=None, trailing_stop_pct=None, opened_at=datetime.utcnow(), strategy_name="test",
    )

    loop.engage_kill_switch_and_flatten("test halt")
    loop._process_all_candidates(_IN_HOURS_NOW)  # submits the close -- stays pending
    assert "TEST" in loop._pending_exit_orders
    orders_placed_after_tick_1 = len(broker._orders)

    loop._process_all_candidates(_IN_HOURS_NOW)  # must NOT submit a second close
    assert len(broker._orders) == orders_placed_after_tick_1


def test_kill_switch_flatten_still_cancels_a_resting_bracket_not_just_pending_exits():
    # Directly answers a real question about the pending-exit guard above:
    # a position with only a resting broker-side stop/target order (the
    # normal, healthy state -- position.broker_stop_order_id set, NOT in
    # self._pending_exit_orders) is a completely different thing from a
    # position whose close is already in flight. The guard must never
    # confuse the two -- the resting bracket still gets cancelled and the
    # market close still gets submitted, exactly as before that guard
    # existed.
    broker = _RestingBroker(fills_after_polls=1)
    loop, candidate = _armed_candidate_setup(broker)
    from webull_bot.state_machine import transition

    transition(candidate, CandidateState.TRIGGERED)
    transition(candidate, CandidateState.ENTERED)
    transition(candidate, CandidateState.MANAGING)
    position = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=10, avg_entry_price=5.00,
        stop_price=4.50, target_price=None, trailing_stop_pct=None,
        opened_at=datetime.utcnow(), strategy_name="test",
    )
    loop._positions["TEST"] = position
    loop._attach_broker_bracket(candidate, position, _IN_HOURS_NOW)
    stop_id = position.broker_stop_order_id
    assert stop_id is not None
    assert "TEST" not in loop._pending_exit_orders  # only a resting bracket, no close in flight

    loop.engage_kill_switch_and_flatten("test halt")
    loop._process_all_candidates(_IN_HOURS_NOW)

    assert stop_id in broker._cancelled  # resting bracket cancelled, not skipped
    assert "TEST" not in loop._positions  # and the position actually closed


# -- end-of-core-hours auto-flatten (distinct from the kill switch: no
# risk_engine.kill_switch_active flip, just closes anything still open once
# the regular 9:30am-4:00pm ET session ends) -------------------------------

_AFTER_CLOSE_NOW = datetime(2026, 8, 10, 21, 0, 0)  # 5:00pm ET, well after the 4:00pm close


def test_open_position_is_untouched_during_core_hours():
    broker = PaperBrokerClient()
    broker.connect()
    loop = _build_loop(broker)
    _managing_candidate_with_position(loop, broker)

    loop._process_all_candidates(_IN_HOURS_NOW)

    assert "TEST" in loop._positions
    assert loop.risk_engine.kill_switch_active is False


def test_open_position_is_auto_flattened_after_core_hours_close():
    broker = PaperBrokerClient()
    broker.connect()
    loop = _build_loop(broker)
    _managing_candidate_with_position(loop, broker)

    loop._process_all_candidates(_AFTER_CLOSE_NOW)

    assert "TEST" not in loop._positions
    assert len(loop._trades) == 1
    assert loop._trades[0].exit_reason == ExitReason.END_OF_CORE_HOURS
    # Unlike the kill switch, this never halts future trading -- it's a
    # one-time end-of-day flatten, not a sticky halt a human must clear.
    assert loop.risk_engine.kill_switch_active is False


def test_auto_flatten_closes_multiple_positions_after_close():
    broker = PaperBrokerClient()
    broker.connect()
    loop = _build_loop(broker)
    _managing_candidate_with_position(loop, broker, symbol="AAA", price=10.0)
    _managing_candidate_with_position(loop, broker, symbol="BBB", price=20.0)

    loop._process_all_candidates(_AFTER_CLOSE_NOW)

    assert loop._positions == {}
    assert {t.symbol for t in loop._trades} == {"AAA", "BBB"}
    assert all(t.exit_reason == ExitReason.END_OF_CORE_HOURS for t in loop._trades)


def test_auto_flatten_is_a_no_op_once_no_positions_remain():
    # Cheap to call every tick after the close: once _positions is empty,
    # this is just an empty loop, not a repeated no-op broker call.
    broker = PaperBrokerClient()
    broker.connect()
    loop = _build_loop(broker)

    loop._process_all_candidates(_AFTER_CLOSE_NOW)  # nothing open; must not raise

    assert loop._positions == {}
    assert loop._trades == []


# 19:58 UTC = 3:58pm ET -- inside the default 2-minute buffer, but BEFORE
# the actual 4:00pm close. Confirmed live 2026-08-11 that firing the
# flatten at/after the real close submits its exit order into an already-
# ended CORE session, which Webull rejects outright -- see
# market_hours.is_within_closing_buffer's docstring.
_BUFFER_WINDOW_NOW = datetime(2026, 8, 10, 19, 58, 0)


def test_open_position_is_flattened_inside_the_pre_close_buffer_not_just_after_the_close():
    broker = PaperBrokerClient()
    broker.connect()
    loop = _build_loop(broker)
    _managing_candidate_with_position(loop, broker)

    loop._process_all_candidates(_BUFFER_WINDOW_NOW)

    assert "TEST" not in loop._positions
    assert len(loop._trades) == 1
    assert loop._trades[0].exit_reason == ExitReason.END_OF_CORE_HOURS


def test_open_position_is_untouched_before_the_pre_close_buffer_starts():
    # 19:50 UTC = 3:50pm ET -- 10 minutes before close, outside the default
    # 2-minute buffer. Still core hours, still not time to flatten yet.
    broker = PaperBrokerClient()
    broker.connect()
    loop = _build_loop(broker)
    _managing_candidate_with_position(loop, broker)

    loop._process_all_candidates(datetime(2026, 8, 10, 19, 50, 0))

    assert "TEST" in loop._positions
    assert loop._trades == []


# -- reconcile_positions_from_broker (startup adoption of a position this
# process doesn't already know about -- closes a restart-survivability gap
# distinct from, but symptomatically identical to, the get_positions()
# parsing-failure incident: either way the bot ends up blind to a position
# that's genuinely open at the broker) --------------------------------------

def test_reconcile_adopts_an_untracked_long_position():
    broker = _FakeBroker()
    broker._positions.append(Position(
        symbol="TEST", side=OrderSide.BUY, quantity=100, avg_entry_price=5.00, stop_price=None,
        target_price=None, trailing_stop_pct=None, opened_at=datetime.utcnow(), strategy_name="unknown",
    ))
    loop, _ = _armed_candidate_setup(broker)
    # _armed_candidate_setup already inserted a "TEST" candidate in ARMED --
    # simulate the real restart scenario more precisely: no candidate at all.
    loop.candidates.clear()

    loop.reconcile_positions_from_broker(_IN_HOURS_NOW)

    assert "TEST" in loop._positions
    position = loop._positions["TEST"]
    assert position.strategy_name == "reconciled_at_startup"
    # Flat stop_loss_pct-as-distance formula (2026-08-11) -- quantity/equity
    # play no role at all, only the current price and RiskConfig.stop_loss_pct
    # (default 5.0) / min_risk_reward_ratio (default 2.0) do. See
    # reconcile_positions_from_broker's docstring.
    assert position.stop_price == pytest.approx(5.20 * (1 - 0.05))
    assert position.target_price == pytest.approx(5.20 * (1 + 0.05 * 2.0))

    assert "TEST" in loop.candidates
    assert loop.candidates["TEST"].state == CandidateState.MANAGING


def test_reconcile_computes_a_short_stop_above_current_price():
    broker = _FakeBroker()
    broker._positions.append(Position(
        symbol="TEST", side=OrderSide.SELL_SHORT, quantity=50, avg_entry_price=6.00, stop_price=None,
        target_price=None, trailing_stop_pct=None, opened_at=datetime.utcnow(), strategy_name="unknown",
    ))
    loop, _ = _armed_candidate_setup(broker)
    loop.candidates.clear()

    loop.reconcile_positions_from_broker(_IN_HOURS_NOW)

    # Same flat stop_loss_pct-as-distance formula as the long test above,
    # mirrored for a short: stop above price, target below. Confirmed fixed
    # 2026-08-11 (back when this was still equity/quantity-based): this
    # guard originally only checked the long side, so a short in a
    # degenerate case got a nonsensical stop/target pair instead of falling
    # back -- the flat formula below sidesteps that whole class of bug by
    # never depending on quantity/equity to begin with.
    assert loop._positions["TEST"].stop_price == pytest.approx(5.20 * (1 + 0.05))
    assert loop._positions["TEST"].target_price == pytest.approx(5.20 * (1 - 0.05 * 2.0))


def test_reconcile_stop_and_target_are_independent_of_quantity_and_equity():
    # Position sizing at entry time depends on max_position_size_pct, but
    # adoption at startup never had a real entry -- there's no signal, no
    # sizing decision, nothing to reconstruct. The flat stop_loss_pct
    # formula (2026-08-11) reflects that: two positions in the same symbol
    # at the same price get an identical stop/target regardless of how many
    # shares are actually held or what the account's equity happens to be.
    broker_small = _FakeBroker(equity=25_000.0)
    broker_small._positions.append(Position(
        symbol="TEST", side=OrderSide.BUY, quantity=5, avg_entry_price=5.00, stop_price=None,
        target_price=None, trailing_stop_pct=None, opened_at=datetime.utcnow(), strategy_name="unknown",
    ))
    loop_small, _ = _armed_candidate_setup(broker_small)
    loop_small.candidates.clear()
    loop_small.reconcile_positions_from_broker(_IN_HOURS_NOW)

    broker_large = _FakeBroker(equity=250_000.0)
    broker_large._positions.append(Position(
        symbol="TEST2", side=OrderSide.BUY, quantity=50_000, avg_entry_price=5.00, stop_price=None,
        target_price=None, trailing_stop_pct=None, opened_at=datetime.utcnow(), strategy_name="unknown",
    ))
    loop_large, _ = _armed_candidate_setup(broker_large)
    loop_large.candidates.clear()
    loop_large.reconcile_positions_from_broker(_IN_HOURS_NOW)

    small = loop_small._positions["TEST"]
    large = loop_large._positions["TEST2"]
    assert small.stop_price == pytest.approx(large.stop_price) == pytest.approx(5.20 * (1 - 0.05))
    assert small.target_price == pytest.approx(large.target_price) == pytest.approx(5.20 * (1 + 0.05 * 2.0))


def test_reconcile_never_overwrites_a_position_already_tracked_this_process():
    broker = _FakeBroker()
    broker._positions.append(Position(
        symbol="TEST", side=OrderSide.BUY, quantity=100, avg_entry_price=5.00, stop_price=None,
        target_price=None, trailing_stop_pct=None, opened_at=datetime.utcnow(), strategy_name="unknown",
    ))
    loop, _ = _armed_candidate_setup(broker)
    loop.candidates.clear()
    already_tracked = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=100, avg_entry_price=5.00, stop_price=4.75,
        target_price=5.50, trailing_stop_pct=None, opened_at=datetime.utcnow(), strategy_name="momentum_breakout",
    )
    loop._positions["TEST"] = already_tracked

    loop.reconcile_positions_from_broker(_IN_HOURS_NOW)

    assert loop._positions["TEST"] is already_tracked
    assert loop._positions["TEST"].strategy_name == "momentum_breakout"


def test_reconcile_leaves_an_already_managing_candidate_alone():
    broker = _FakeBroker()
    broker._positions.append(Position(
        symbol="TEST", side=OrderSide.BUY, quantity=100, avg_entry_price=5.00, stop_price=None,
        target_price=None, trailing_stop_pct=None, opened_at=datetime.utcnow(), strategy_name="unknown",
    ))
    loop, candidate = _armed_candidate_setup(broker)
    from webull_bot.state_machine import transition as _transition
    for state in (CandidateState.TRIGGERED, CandidateState.ENTERED, CandidateState.MANAGING):
        _transition(candidate, state)

    loop.reconcile_positions_from_broker(_IN_HOURS_NOW)

    # Still the exact same Candidate object, not replaced with a fresh one.
    assert loop.candidates["TEST"] is candidate
    assert candidate.state == CandidateState.MANAGING


def test_reconcile_rebuilds_a_candidate_stuck_in_a_non_terminal_state():
    # Real incident: a candidate sitting in TRIGGERED (the exact case
    # adoption exists for -- an entry filled at the broker but this
    # process never confirmed it) made the old code's single-hop
    # transition straight to MANAGING illegal (only ENTERED -> MANAGING is
    # a legal hop -- see state_machine._ALLOWED_TRANSITIONS). That raised
    # InvalidStateTransition and aborted reconciliation for every symbol
    # still left in that pass -- confirmed live as the reason candidates
    # stayed stuck in TRIGGERED indefinitely, since the very mechanism
    # meant to unstick them kept crashing on the attempt.
    broker = _FakeBroker()
    broker._positions.append(Position(
        symbol="TEST", side=OrderSide.BUY, quantity=100, avg_entry_price=5.00, stop_price=None,
        target_price=None, trailing_stop_pct=None, opened_at=datetime.utcnow(), strategy_name="unknown",
    ))
    loop, candidate = _armed_candidate_setup(broker)
    from webull_bot.state_machine import transition as _transition_stuck
    _transition_stuck(candidate, CandidateState.TRIGGERED)
    loop._persisted_transition_counts["TEST"] = 3

    loop.reconcile_positions_from_broker(_IN_HOURS_NOW)  # must not raise

    assert "TEST" in loop._positions
    assert loop.candidates["TEST"].state == CandidateState.MANAGING
    # A fresh Candidate object, not the stuck one advanced illegally --
    # and its persisted-transition count reset so _flush_state_transitions
    # persists the new object's full history rather than skipping entries
    # it thinks are already covered.
    assert loop.candidates["TEST"] is not candidate
    assert "TEST" not in loop._persisted_transition_counts


def test_reconcile_rebuilds_a_candidate_stuck_in_armed():
    # Same class of bug, different starting state: ARMED can't jump
    # straight to MANAGING either (only TRIGGERED/HEATING_UP/REJECTED/
    # COOLDOWN are legal from ARMED).
    broker = _FakeBroker()
    broker._positions.append(Position(
        symbol="TEST", side=OrderSide.BUY, quantity=100, avg_entry_price=5.00, stop_price=None,
        target_price=None, trailing_stop_pct=None, opened_at=datetime.utcnow(), strategy_name="unknown",
    ))
    loop, candidate = _armed_candidate_setup(broker)  # already leaves candidate in ARMED

    loop.reconcile_positions_from_broker(_IN_HOURS_NOW)  # must not raise

    assert "TEST" in loop._positions
    assert loop.candidates["TEST"].state == CandidateState.MANAGING


def test_reconcile_is_a_no_op_when_get_positions_fails():
    class _BrokenBroker(_FakeBroker):
        def get_positions(self):
            raise RuntimeError("simulated broker outage")

    broker = _BrokenBroker()
    loop, _ = _armed_candidate_setup(broker)
    loop.candidates.clear()

    loop.reconcile_positions_from_broker(_IN_HOURS_NOW)  # must not raise

    assert loop._positions == {}
    assert loop.candidates == {}


def test_reconcile_skips_a_symbol_whose_snapshot_fails():
    class _NoSnapshotBroker(_FakeBroker):
        def get_snapshot(self, symbol):
            raise RuntimeError("simulated quote failure")

    broker = _NoSnapshotBroker()
    broker._positions.append(Position(
        symbol="TEST", side=OrderSide.BUY, quantity=100, avg_entry_price=5.00, stop_price=None,
        target_price=None, trailing_stop_pct=None, opened_at=datetime.utcnow(), strategy_name="unknown",
    ))
    loop, _ = _armed_candidate_setup(broker)
    loop.candidates.clear()

    loop.reconcile_positions_from_broker(_IN_HOURS_NOW)  # must not raise

    assert loop._positions == {}
    assert loop.candidates == {}


def test_reconcile_drops_a_position_no_longer_at_the_broker():
    # Real incident: scripts/list_and_close_positions.py closes a position
    # by calling broker.place_order directly, entirely outside this
    # running process -- the dashboard kept showing it as open indefinitely
    # afterward since nothing ever told the bot it was gone.
    from webull_bot.state_machine import transition
    from webull_bot.enums import CandidateState

    broker = _FakeBroker()  # no positions -- simulates the broker-side close
    loop, candidate = _armed_candidate_setup(broker)
    transition(candidate, CandidateState.TRIGGERED)
    transition(candidate, CandidateState.ENTERED)
    transition(candidate, CandidateState.MANAGING)
    loop._positions["TEST"] = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=10, avg_entry_price=5.00, stop_price=4.50,
        target_price=None, trailing_stop_pct=None, opened_at=datetime.utcnow(), strategy_name="test",
    )

    loop.reconcile_positions_from_broker(_IN_HOURS_NOW)

    assert "TEST" not in loop._positions
    assert candidate.state.value == "cooldown"


def test_reconcile_does_not_drop_a_position_with_a_pending_exit_in_flight():
    # This process's own exit is already submitted for this symbol --
    # dropping it here would race _poll_pending_exit/_dispatch_exit_finalization,
    # which needs the Position object to still exist to finalize the trade.
    from webull_bot.state_machine import transition
    from webull_bot.enums import CandidateState
    from webull_bot.models import Signal
    from webull_bot.enums import SignalAction

    broker = _FakeBroker()  # broker-side quantity already at 0 for this symbol
    loop, candidate = _armed_candidate_setup(broker)
    transition(candidate, CandidateState.TRIGGERED)
    transition(candidate, CandidateState.ENTERED)
    transition(candidate, CandidateState.MANAGING)
    position = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=10, avg_entry_price=5.00, stop_price=4.50,
        target_price=None, trailing_stop_pct=None, opened_at=datetime.utcnow(), strategy_name="test",
    )
    loop._positions["TEST"] = position
    pending_order = Order(
        symbol="TEST", side=OrderSide.SELL, order_type=OrderType.MARKET, quantity=10,
        status=OrderStatus.SUBMITTED, broker_order_id="ord-1",
    )
    exit_signal = Signal(
        symbol="TEST", action=SignalAction.EXIT, generated_at=datetime.utcnow(),
        strategy_name="test", strategy_version="v1", reference_price=4.50,
    )
    loop._pending_exit_orders["TEST"] = (pending_order, exit_signal)

    loop.reconcile_positions_from_broker(_IN_HOURS_NOW)

    assert "TEST" in loop._positions
    assert candidate.state.value == "managing"


def test_run_forever_reconciles_positions_before_entering_the_loop():
    broker = PaperBrokerClient()
    broker.connect()
    loop = _build_loop(broker)

    calls = []
    loop.reconcile_positions_from_broker = lambda *a, **kw: calls.append(True)

    stop_event = threading.Event()
    runner = threading.Thread(target=loop.run_forever, kwargs={"stop_flag": stop_event.is_set})
    runner.start()
    try:
        time_module.sleep(0.1)
    finally:
        stop_event.set()
        runner.join(timeout=2)

    assert calls == [True]


def test_process_all_candidates_reconciles_immediately_then_throttles():
    broker = PaperBrokerClient()
    broker.connect()
    loop = _build_loop(broker)

    calls = []
    loop.reconcile_positions_from_broker = lambda *a, **kw: calls.append(True)

    loop._process_all_candidates(_IN_HOURS_NOW)
    assert calls == [True]  # fires immediately -- _last_position_reconcile started unset

    loop._process_all_candidates(_IN_HOURS_NOW + timedelta(seconds=1))
    assert calls == [True]  # well within the 30s default interval -- no second call yet

    loop._process_all_candidates(
        _IN_HOURS_NOW + timedelta(seconds=loop.config.position_reconcile_interval_seconds + 1)
    )
    assert calls == [True, True]


# -- per-tick get_positions() dedup (_get_positions_for_tick) ---------------

def test_get_positions_for_tick_shares_one_broker_call_within_a_pass():
    class _CountingBroker(_FakeBroker):
        get_positions_calls: int = 0

        def get_positions(self):
            self.get_positions_calls += 1
            return super().get_positions()

    broker = _CountingBroker()
    loop, candidate = _armed_candidate_setup(broker)

    first = loop._get_positions_for_tick()
    second = loop._get_positions_for_tick()

    assert first is second  # exact same list object, not just equal
    assert broker.get_positions_calls == 1


def test_tick_positions_cache_resets_between_passes():
    # Reconcile alone triggers one broker.get_positions() call per
    # _process_all_candidates pass (it always fires -- see
    # test_process_all_candidates_reconciles_immediately_then_throttles)
    # via _get_positions_for_tick -- exactly one per pass, not accumulating
    # or staying stuck at the first pass' cached value.
    class _CountingBroker(_FakeBroker):
        get_positions_calls: int = 0

        def get_positions(self):
            self.get_positions_calls += 1
            return super().get_positions()

    broker = _CountingBroker()
    loop, candidate = _armed_candidate_setup(broker)

    loop._process_all_candidates(_IN_HOURS_NOW)
    assert broker.get_positions_calls == 1

    # Space this second pass out past position_reconcile_interval_seconds
    # (30s default) -- reconcile is throttled, so a pass too soon after the
    # first wouldn't call get_positions() again at all, which would make
    # "the cache resets" indistinguishable from "reconcile just didn't run
    # this time." See test_process_all_candidates_reconciles_immediately_then_throttles.
    loop._process_all_candidates(
        _IN_HOURS_NOW + timedelta(seconds=loop.config.position_reconcile_interval_seconds + 1)
    )
    assert broker.get_positions_calls == 2


def test_maybe_verify_entry_via_positions_and_reconcile_share_the_tick_cache():
    # The exact real-world scenario this exists for: a TRIGGERED entry
    # crossing its own verify-delay threshold in the same pass as the
    # (independently-throttled) periodic reconcile -- both want
    # get_positions(), but only one real network call should happen.
    class _CountingEmptyBroker(_FakeBroker):
        get_positions_calls: int = 0

        def get_positions(self):
            # Never shows a matching position for TEST -- keeps the entry
            # genuinely pending (not filled) so this test isolates the
            # cache-sharing behavior from _confirm_entry_filled's own
            # deliberately-uncached follow-up lookup (see
            # _get_positions_for_tick's docstring for why that one stays
            # direct).
            self.get_positions_calls += 1
            return []

    broker = _CountingEmptyBroker(fills_after_polls=1000)
    loop, candidate = _armed_candidate_setup(broker)
    snapshot = _snapshot(_IN_HOURS_NOW, 5.20, 5.20, 600_000, 5.19, 5.21, 5.15)
    signal = _trigger_and_build_signal(candidate, snapshot)
    loop._submit_entry(candidate, signal, snapshot, snapshot.timestamp)
    broker.get_positions_calls = 0  # ignore submit_entry's own risk-sizing lookup

    later = snapshot.timestamp + timedelta(seconds=11)
    loop._maybe_verify_entry_via_positions(candidate, loop._pending_entry_orders["TEST"], later)
    loop.reconcile_positions_from_broker(later)

    assert broker.get_positions_calls == 1
    assert "TEST" in loop._pending_entry_orders  # still genuinely pending, not filled


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
    batch_priorities: list = field(default_factory=list)

    def get_snapshot(self, symbol):
        self.individual_calls.append(symbol)
        return super().get_snapshot(symbol)

    def get_snapshots(self, symbols, priority=None):
        self.batch_calls.append(list(symbols))
        self.batch_priorities.append(priority)
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

    loop._process_all_candidates(_IN_HOURS_NOW)

    assert broker.batch_calls == [["ONE", "TWO"]]
    assert broker.individual_calls == []  # neither candidate fell back to its own get_snapshot()
    assert loop.candidates["ONE"].last_price == 5.20
    assert loop.candidates["TWO"].last_price == 5.20


def test_process_all_candidates_requests_critical_priority_for_its_batch_snapshot_call():
    # This batch feeds MANAGING positions' stop/target/VWAP checks directly
    # -- must win contention over BroadScanner's own (BACKGROUND-priority)
    # discovery snapshot calls. See retry.py's CallPriority docstring.
    from webull_bot.brokers.webull.retry import CallPriority

    broker = _BatchAwareBroker()
    loop = _two_candidate_loop(broker)

    loop._process_all_candidates(_IN_HOURS_NOW)

    assert broker.batch_priorities == [CallPriority.CRITICAL]


def test_process_all_candidates_falls_back_without_get_snapshots():
    # _FakeBroker has no get_snapshots at all -- representative of
    # PaperBrokerClient/any broker that doesn't support batching.
    broker = _FakeBroker()
    loop = _two_candidate_loop(broker)

    loop._process_all_candidates(_IN_HOURS_NOW)

    assert loop.candidates["ONE"].last_price == 5.20
    assert loop.candidates["TWO"].last_price == 5.20


def test_process_all_candidates_falls_back_when_batch_call_raises():
    class _FlakyBatchBroker(_BatchAwareBroker):
        def get_snapshots(self, symbols, priority=None):
            self.batch_calls.append(list(symbols))
            raise RuntimeError("simulated Webull batch failure")

    broker = _FlakyBatchBroker()
    loop = _two_candidate_loop(broker)

    loop._process_all_candidates(_IN_HOURS_NOW)

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

    loop._process_all_candidates(_IN_HOURS_NOW)

    assert broker.batch_calls == [["ONE", "TWO"]]  # THREE (COOLDOWN) excluded


# -- broker-side (resting) stop/target bracket -------------------------------

def test_attach_broker_bracket_places_stop_and_target_when_supported():
    broker = _RestingBroker()
    loop, candidate = _armed_candidate_setup(broker)
    position = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=10, avg_entry_price=5.00,
        stop_price=4.50, target_price=5.50, trailing_stop_pct=None,
        opened_at=datetime.utcnow(), strategy_name="test",
    )

    loop._attach_broker_bracket(candidate, position, _IN_HOURS_NOW)

    assert position.broker_stop_order_id is not None
    assert position.broker_target_order_id is not None
    assert position.broker_stop_price_synced == 4.50
    assert len(broker._brackets) == 1
    stop_order, target_order = broker._brackets[0]
    assert stop_order.side == OrderSide.SELL
    assert stop_order.quantity == 10
    assert stop_order.stop_price == 4.50
    assert target_order.quantity == 5  # floored half
    assert target_order.limit_price == 5.50


def test_attach_broker_bracket_is_a_noop_outside_core_hours():
    # Real incident, 2026-08-12: a resting OCO stop+target bracket was
    # rejected pre-market with support_trading_session="ALL" (the same
    # OAUTH_OPENAPI_PARAM_ERR as the original 2026-08-10 finding), and
    # _sync_broker_protective_orders' every-tick retry kept re-attempting
    # and re-failing this call, burning CRITICAL-priority rate-limiter
    # budget every poll cycle and starving candidate discovery behind it.
    # Outside core hours this must now no-op BEFORE any broker call --
    # confirmed here by asserting the broker's own bracket-tracking list
    # stays empty, not just that the position ends up unbracketed (which a
    # failed API call would also produce).
    broker = _RestingBroker()
    loop, candidate = _armed_candidate_setup(broker)
    position = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=10, avg_entry_price=5.00,
        stop_price=4.50, target_price=5.50, trailing_stop_pct=None,
        opened_at=datetime.utcnow(), strategy_name="test",
    )

    loop._attach_broker_bracket(candidate, position, _PRE_MARKET_NOW)

    assert position.broker_stop_order_id is None
    assert position.broker_target_order_id is None
    assert len(broker._brackets) == 0


def test_attach_broker_bracket_places_lone_stop_when_no_target():
    # target_price=None here because it's too small to split into two
    # whole-share halves (see _attach_broker_bracket's docstring) --
    # partial_exit_taken is still False (defaults False, not passed), so
    # this must NOT get a trailing stop either: a position that never
    # takes a partial rides on a plain stop + breakeven for its whole
    # lifetime (see PositionManager.check_exit's docstring). See
    # test_attach_broker_bracket_uses_trailing_stop_once_partial_exit_taken
    # below for the genuinely-post-partial case, which DOES get a trailing
    # stop.
    broker = _RestingBroker()
    loop, candidate = _armed_candidate_setup(broker)
    position = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=10, avg_entry_price=5.00,
        stop_price=4.50, target_price=None, trailing_stop_pct=None,
        opened_at=datetime.utcnow(), strategy_name="test",
    )

    loop._attach_broker_bracket(candidate, position, _IN_HOURS_NOW)

    assert position.broker_stop_order_id is not None
    assert position.broker_target_order_id is None
    assert position.broker_stop_is_trailing is False
    assert broker._brackets == []
    assert len(broker._lone_stops) == 1
    assert broker._lone_stops[0].order_type == OrderType.STOP


def test_attach_broker_bracket_is_a_noop_without_broker_support():
    # PaperBrokerClient/backtests fill everything synchronously at market --
    # no place_oco_bracket at all -- see order_manager.py's
    # _broker_supports_resting_orders. _FakeBroker (used throughout this
    # file's other tests) mirrors that: no place_oco_bracket either.
    broker = _FakeBroker()
    loop, candidate = _armed_candidate_setup(broker)
    position = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=10, avg_entry_price=5.00,
        stop_price=4.50, target_price=5.50, trailing_stop_pct=None,
        opened_at=datetime.utcnow(), strategy_name="test",
    )

    loop._attach_broker_bracket(candidate, position, _IN_HOURS_NOW)

    assert position.broker_stop_order_id is None
    assert position.broker_target_order_id is None


def test_attach_broker_bracket_never_rearms_target_after_partial_exit_taken():
    broker = _RestingBroker()
    loop, candidate = _armed_candidate_setup(broker)
    position = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=5, avg_entry_price=5.00,
        stop_price=4.80, target_price=5.50, trailing_stop_pct=None,
        opened_at=datetime.utcnow(), strategy_name="test", partial_exit_taken=True,
    )

    loop._attach_broker_bracket(candidate, position, _IN_HOURS_NOW)

    assert position.broker_target_order_id is None
    assert broker._brackets == []
    assert len(broker._lone_stops) == 1


def test_attach_broker_bracket_uses_trailing_stop_once_partial_exit_taken():
    # This is the genuinely-post-partial case (unlike the
    # too-small-to-split case in test_attach_broker_bracket_places_lone_stop_when_no_target
    # above, which also has target_price=None but partial_exit_taken=False)
    # -- the remainder gets protected with a native broker-side
    # TRAILING_STOP instead of a plain STOP, using the live
    # PositionManager.config.trailing_stop_pct (default 3.0).
    broker = _RestingBroker()
    loop, candidate = _armed_candidate_setup(broker)
    position = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=5, avg_entry_price=5.00,
        stop_price=4.80, target_price=5.50, trailing_stop_pct=None,
        opened_at=datetime.utcnow(), strategy_name="test", partial_exit_taken=True,
    )

    loop._attach_broker_bracket(candidate, position, _IN_HOURS_NOW)

    assert position.broker_stop_order_id is not None
    assert position.broker_target_order_id is None
    assert position.broker_stop_is_trailing is True
    assert len(broker._lone_stops) == 1
    trailing_order = broker._lone_stops[0]
    assert trailing_order.order_type == OrderType.TRAILING_STOP
    assert trailing_order.trailing_pct == 3.0


def test_attach_broker_bracket_falls_back_to_plain_stop_when_trailing_disabled():
    broker = _RestingBroker()
    loop, candidate = _armed_candidate_setup(broker)
    loop.position_manager.config.trailing_stop_pct = None  # trailing rule disabled
    position = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=5, avg_entry_price=5.00,
        stop_price=4.80, target_price=5.50, trailing_stop_pct=None,
        opened_at=datetime.utcnow(), strategy_name="test", partial_exit_taken=True,
    )

    loop._attach_broker_bracket(candidate, position, _IN_HOURS_NOW)

    assert position.broker_stop_is_trailing is False
    assert len(broker._lone_stops) == 1
    assert broker._lone_stops[0].order_type == OrderType.STOP


def test_attach_broker_bracket_cancels_a_leftover_resting_order_before_going_trailing():
    # Defensive belt-and-suspenders case (see _attach_broker_bracket's
    # docstring): if a resting order was somehow still attached when this
    # transitions to trailing, it must be cancelled first -- a
    # TRAILING_STOP can't be added while another resting sell order still
    # reserves the same shares.
    broker = _RestingBroker()
    loop, candidate = _armed_candidate_setup(broker)
    position = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=5, avg_entry_price=5.00,
        stop_price=4.80, target_price=5.50, trailing_stop_pct=None,
        opened_at=datetime.utcnow(), strategy_name="test", partial_exit_taken=True,
    )
    position.broker_stop_order_id = "stale-stop-id"

    loop._attach_broker_bracket(candidate, position, _IN_HOURS_NOW)

    assert "stale-stop-id" in broker._cancelled
    assert position.broker_stop_is_trailing is True


def test_confirm_entry_filled_attaches_broker_bracket_when_supported():
    from webull_bot.enums import SignalAction
    from webull_bot.models import Signal
    from webull_bot.state_machine import transition

    broker = _RestingBroker(fills_after_polls=1)
    loop, candidate = _armed_candidate_setup(broker)
    snapshot = _snapshot(_IN_HOURS_NOW, 5.20, 5.20, 600_000, 5.19, 5.21, 5.15)
    transition(candidate, CandidateState.TRIGGERED)
    signal = Signal(
        symbol="TEST", action=SignalAction.ENTER_LONG, generated_at=snapshot.timestamp,
        strategy_name="test", strategy_version="v1", reference_price=5.20,
        suggested_stop=5.00, suggested_target=5.60,
    )

    loop._submit_entry(candidate, signal, snapshot, snapshot.timestamp)
    loop._poll_pending_entry(candidate, snapshot.timestamp)  # fills_after_polls=1

    assert "TEST" in loop._positions
    position = loop._positions["TEST"]
    assert position.broker_stop_order_id is not None
    assert position.broker_target_order_id is not None


# -- batched broker-bracket status polling (_get_open_orders_for_tick) -----

def test_poll_broker_bracket_skips_individual_calls_when_still_resting_per_batch():
    broker = _RestingBroker()
    loop, candidate = _armed_candidate_setup(broker)
    from webull_bot.state_machine import transition

    transition(candidate, CandidateState.TRIGGERED)
    transition(candidate, CandidateState.ENTERED)
    transition(candidate, CandidateState.MANAGING)

    position = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=10, avg_entry_price=5.00,
        stop_price=4.50, target_price=5.50, trailing_stop_pct=None,
        opened_at=datetime.utcnow(), strategy_name="test",
    )
    loop._positions["TEST"] = position
    loop._attach_broker_bracket(candidate, position, _IN_HOURS_NOW)
    assert position.broker_stop_order_id is not None
    assert position.broker_target_order_id is not None
    broker.get_order_status_calls = 0  # ignore any calls from attaching the bracket itself

    handled = loop._poll_broker_bracket(candidate, position, _IN_HOURS_NOW)

    assert handled is False
    assert broker.list_open_orders_calls == 1
    # Both legs came back in the batch as still SUBMITTED -- neither needed
    # its own get_order_status call.
    assert broker.get_order_status_calls == 0


def test_poll_broker_bracket_falls_back_to_individual_call_for_a_leg_missing_from_the_batch():
    broker = _RestingBroker()
    loop, candidate = _armed_candidate_setup(broker)
    from webull_bot.state_machine import transition

    transition(candidate, CandidateState.TRIGGERED)
    transition(candidate, CandidateState.ENTERED)
    transition(candidate, CandidateState.MANAGING)

    position = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=10, avg_entry_price=5.00,
        stop_price=4.50, target_price=None, trailing_stop_pct=None,
        opened_at=datetime.utcnow(), strategy_name="test",
    )
    loop._positions["TEST"] = position
    loop._attach_broker_bracket(candidate, position, _IN_HOURS_NOW)
    stop_id = position.broker_stop_order_id
    broker._resting_orders[stop_id].status = OrderStatus.FILLED
    broker._resting_orders[stop_id].quantity = 10
    broker.get_order_status_calls = 0

    trades = []
    loop.on_trade_closed = trades.append
    handled = loop._poll_broker_bracket(candidate, position, _IN_HOURS_NOW)

    assert handled is True
    assert len(trades) == 1
    # The batch correctly omitted the now-FILLED leg (see _RestingBroker.
    # list_open_orders), so exactly one targeted get_order_status call was
    # needed to learn it specifically filled (vs. cancelled) -- not zero,
    # not the original two-calls-per-tick.
    assert broker.get_order_status_calls == 1


def test_poll_broker_bracket_falls_back_entirely_without_list_open_orders_support():
    class _RestingBrokerWithoutBatchPolling(_RestingBroker):
        list_open_orders = None  # removes the capability -- getattr(..., None) finds this and stops

    broker = _RestingBrokerWithoutBatchPolling()
    loop, candidate = _armed_candidate_setup(broker)
    from webull_bot.state_machine import transition

    transition(candidate, CandidateState.TRIGGERED)
    transition(candidate, CandidateState.ENTERED)
    transition(candidate, CandidateState.MANAGING)

    position = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=10, avg_entry_price=5.00,
        stop_price=4.50, target_price=5.50, trailing_stop_pct=None,
        opened_at=datetime.utcnow(), strategy_name="test",
    )
    loop._positions["TEST"] = position
    loop._attach_broker_bracket(candidate, position, _IN_HOURS_NOW)
    broker.get_order_status_calls = 0

    handled = loop._poll_broker_bracket(candidate, position, _IN_HOURS_NOW)

    assert handled is False
    # No batching capability at all -- both legs fall back to their own
    # individual get_order_status call, exactly the pre-2026-08-11 behavior.
    assert broker.get_order_status_calls == 2


def test_get_open_orders_for_tick_shares_one_broker_call_within_a_pass():
    broker = _RestingBroker()
    loop, candidate = _armed_candidate_setup(broker)
    position = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=10, avg_entry_price=5.00,
        stop_price=4.50, target_price=None, trailing_stop_pct=None,
        opened_at=datetime.utcnow(), strategy_name="test",
    )
    loop._attach_broker_bracket(candidate, position, _IN_HOURS_NOW)
    broker.list_open_orders_calls = 0

    first = loop._get_open_orders_for_tick()
    second = loop._get_open_orders_for_tick()

    assert first is second
    assert broker.list_open_orders_calls == 1


def test_poll_broker_bracket_finalizes_full_exit_on_stop_fill():
    broker = _RestingBroker()
    loop, candidate = _armed_candidate_setup(broker)
    from webull_bot.state_machine import transition

    transition(candidate, CandidateState.TRIGGERED)
    transition(candidate, CandidateState.ENTERED)
    transition(candidate, CandidateState.MANAGING)

    position = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=10, avg_entry_price=5.00,
        stop_price=4.50, target_price=None, trailing_stop_pct=None,
        opened_at=datetime.utcnow(), strategy_name="test",
    )
    loop._positions["TEST"] = position
    loop._attach_broker_bracket(candidate, position, _IN_HOURS_NOW)
    stop_id = position.broker_stop_order_id
    assert stop_id is not None

    # Simulate the resting stop filling at the broker.
    broker._resting_orders[stop_id].status = OrderStatus.FILLED
    broker._resting_orders[stop_id].quantity = 10

    trades = []
    loop.on_trade_closed = trades.append
    handled = loop._poll_broker_bracket(candidate, position, _IN_HOURS_NOW)

    assert handled is True
    assert "TEST" not in loop._positions
    assert candidate.state.value == "cooldown"
    assert len(trades) == 1
    assert trades[0].exit_reason == ExitReason.STOP_LOSS


def test_poll_broker_bracket_finalizes_partial_exit_on_target_fill_and_reprotects_remainder():
    broker = _RestingBroker()
    loop, candidate = _armed_candidate_setup(broker)
    from webull_bot.state_machine import transition

    transition(candidate, CandidateState.TRIGGERED)
    transition(candidate, CandidateState.ENTERED)
    transition(candidate, CandidateState.MANAGING)

    position = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=10, avg_entry_price=5.00,
        stop_price=4.50, target_price=5.50, trailing_stop_pct=None,
        opened_at=datetime.utcnow(), strategy_name="test",
    )
    loop._positions["TEST"] = position
    loop._attach_broker_bracket(candidate, position, _IN_HOURS_NOW)
    target_id = position.broker_target_order_id
    assert target_id is not None
    assert len(broker._brackets) == 1

    broker._resting_orders[target_id].status = OrderStatus.FILLED
    broker._resting_orders[target_id].quantity = 5

    trades = []
    loop.on_trade_closed = trades.append
    handled = loop._poll_broker_bracket(candidate, position, _IN_HOURS_NOW)

    assert handled is True
    assert "TEST" in loop._positions  # remainder stays open
    assert position.quantity == 5
    assert position.partial_exit_taken is True
    assert len(trades) == 1
    assert trades[0].exit_reason == ExitReason.PARTIAL_PROFIT_TARGET
    # The remainder was immediately re-protected with a fresh resting
    # TRAILING_STOP (partial_exit_taken is now True) and no target (never
    # re-armed after one partial -- see _attach_broker_bracket).
    assert position.broker_stop_order_id is not None
    assert position.broker_target_order_id is None
    assert position.broker_stop_is_trailing is True
    assert len(broker._brackets) == 1  # no second bracket -- just a lone stop
    assert len(broker._lone_stops) == 1
    assert broker._lone_stops[0].order_type == OrderType.TRAILING_STOP


def test_sync_broker_protective_orders_replaces_stop_when_price_moved():
    broker = _RestingBroker()
    loop, candidate = _armed_candidate_setup(broker)
    position = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=10, avg_entry_price=5.00,
        stop_price=4.50, target_price=None, trailing_stop_pct=None,
        opened_at=datetime.utcnow(), strategy_name="test",
    )
    loop._attach_broker_bracket(candidate, position, _IN_HOURS_NOW)
    old_stop_id = position.broker_stop_order_id

    position.stop_price = 5.00  # simulate PositionManager's breakeven bump
    loop._sync_broker_protective_orders(candidate, position, _IN_HOURS_NOW)

    assert old_stop_id in broker._cancelled
    assert position.broker_stop_order_id is not None
    assert position.broker_stop_order_id != old_stop_id
    assert position.broker_stop_price_synced == 5.00
    assert broker._resting_orders[position.broker_stop_order_id].stop_price == 5.00


def test_sync_broker_protective_orders_replaces_both_legs_when_target_still_active():
    broker = _RestingBroker()
    loop, candidate = _armed_candidate_setup(broker)
    position = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=10, avg_entry_price=5.00,
        stop_price=4.50, target_price=5.50, trailing_stop_pct=None,
        opened_at=datetime.utcnow(), strategy_name="test",
    )
    loop._attach_broker_bracket(candidate, position, _IN_HOURS_NOW)
    old_stop_id = position.broker_stop_order_id
    old_target_id = position.broker_target_order_id

    position.stop_price = 5.00
    loop._sync_broker_protective_orders(candidate, position, _IN_HOURS_NOW)

    assert old_stop_id in broker._cancelled
    assert old_target_id in broker._cancelled
    assert position.broker_target_order_id is not None
    assert position.broker_target_order_id != old_target_id
    assert len(broker._brackets) == 2  # original + the replacement


def test_sync_broker_protective_orders_is_a_noop_for_a_trailing_stop():
    # Once broker_stop_is_trailing is True, Webull is moving the resting
    # order itself -- there is nothing left for this process to push, no
    # matter how far position.stop_price has moved in software (that
    # software value is purely informational/tracking at this point, see
    # Position.broker_stop_is_trailing's docstring).
    broker = _RestingBroker()
    loop, candidate = _armed_candidate_setup(broker)
    position = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=5, avg_entry_price=5.00,
        stop_price=4.80, target_price=None, trailing_stop_pct=None,
        opened_at=datetime.utcnow(), strategy_name="test", partial_exit_taken=True,
    )
    loop._attach_broker_bracket(candidate, position, _IN_HOURS_NOW)
    assert position.broker_stop_is_trailing is True
    trailing_order_id = position.broker_stop_order_id

    position.stop_price = 5.50  # a large software-side trailing move
    loop._sync_broker_protective_orders(candidate, position, _IN_HOURS_NOW)

    assert trailing_order_id not in broker._cancelled
    assert position.broker_stop_order_id == trailing_order_id


def test_sync_broker_protective_orders_is_a_noop_when_stop_unchanged():
    broker = _RestingBroker()
    loop, candidate = _armed_candidate_setup(broker)
    position = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=10, avg_entry_price=5.00,
        stop_price=4.50, target_price=None, trailing_stop_pct=None,
        opened_at=datetime.utcnow(), strategy_name="test",
    )
    loop._attach_broker_bracket(candidate, position, _IN_HOURS_NOW)
    stop_id = position.broker_stop_order_id

    loop._sync_broker_protective_orders(candidate, position, _IN_HOURS_NOW)

    assert broker._cancelled == []
    assert position.broker_stop_order_id == stop_id


def test_sync_broker_protective_orders_ignores_a_move_below_the_threshold():
    # Hysteresis against trailing-stop tick-to-tick float noise -- see
    # TradingLoopConfig.stop_sync_min_move_pct's docstring. Default 0.25%;
    # a move of ~0.1% must not trigger a cancel+replace.
    broker = _RestingBroker()
    loop, candidate = _armed_candidate_setup(broker)
    position = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=10, avg_entry_price=5.00,
        stop_price=4.50, target_price=None, trailing_stop_pct=None,
        opened_at=datetime.utcnow(), strategy_name="test",
    )
    loop._attach_broker_bracket(candidate, position, _IN_HOURS_NOW)
    stop_id = position.broker_stop_order_id

    position.stop_price = 4.5045  # +0.1% -- below the 0.25% default threshold
    loop._sync_broker_protective_orders(candidate, position, _IN_HOURS_NOW)

    assert broker._cancelled == []
    assert position.broker_stop_order_id == stop_id
    assert position.broker_stop_price_synced == 4.50  # unchanged -- still the last real sync


def test_sync_broker_protective_orders_acts_on_a_move_at_or_above_the_threshold():
    broker = _RestingBroker()
    loop, candidate = _armed_candidate_setup(broker)
    position = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=10, avg_entry_price=5.00,
        stop_price=4.50, target_price=None, trailing_stop_pct=None,
        opened_at=datetime.utcnow(), strategy_name="test",
    )
    loop._attach_broker_bracket(candidate, position, _IN_HOURS_NOW)
    stop_id = position.broker_stop_order_id

    position.stop_price = 4.52  # ~+0.44% -- above the 0.25% default threshold
    loop._sync_broker_protective_orders(candidate, position, _IN_HOURS_NOW)

    assert stop_id in broker._cancelled
    assert position.broker_stop_price_synced == 4.52


def test_stop_sync_min_move_pct_is_configurable():
    broker = _RestingBroker()
    loop, candidate = _armed_candidate_setup(broker)
    loop.config.stop_sync_min_move_pct = 5.0  # much looser than the default
    position = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=10, avg_entry_price=5.00,
        stop_price=4.50, target_price=None, trailing_stop_pct=None,
        opened_at=datetime.utcnow(), strategy_name="test",
    )
    loop._attach_broker_bracket(candidate, position, _IN_HOURS_NOW)
    stop_id = position.broker_stop_order_id

    position.stop_price = 4.60  # ~+2.2% -- would clear the 0.25% default, not this 5% override
    loop._sync_broker_protective_orders(candidate, position, _IN_HOURS_NOW)

    assert broker._cancelled == []
    assert position.broker_stop_order_id == stop_id


def test_sync_broker_protective_orders_is_a_permanent_noop_when_broker_unsupported():
    # _FakeBroker has no place_oco_bracket at all -- unlike a previous
    # failed attempt, this is never going to succeed no matter how many
    # times it's retried, so _sync_broker_protective_orders must not even
    # try (a cheap getattr check, not a real call attempt).
    broker = _FakeBroker()
    loop, candidate = _armed_candidate_setup(broker)
    position = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=10, avg_entry_price=5.00,
        stop_price=4.50, target_price=None, trailing_stop_pct=None,
        opened_at=datetime.utcnow(), strategy_name="test",
    )
    position.stop_price = 5.00
    loop._sync_broker_protective_orders(candidate, position, _IN_HOURS_NOW)
    assert position.broker_stop_order_id is None


def test_sync_broker_protective_orders_retries_a_previously_failed_attach():
    # broker_stop_order_id is None here NOT because the broker lacks
    # support (_RestingBroker has place_oco_bracket) but because an
    # earlier _attach_broker_bracket attempt failed -- e.g. a rate limit
    # or network error at entry-confirm time. Real broker-side protection
    # matters too much to give up on after one failure (see the RDGT
    # incident note on _attach_broker_bracket) -- this must retry and,
    # once it succeeds, the position ends up genuinely broker-managed.
    broker = _RestingBroker()
    loop, candidate = _armed_candidate_setup(broker)
    position = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=10, avg_entry_price=5.00,
        stop_price=4.50, target_price=None, trailing_stop_pct=None,
        opened_at=datetime.utcnow(), strategy_name="test",
    )
    position.stop_price = 5.00
    loop._sync_broker_protective_orders(candidate, position, _IN_HOURS_NOW)
    assert position.broker_stop_order_id is not None
    assert position.broker_stop_price_synced == 5.00


def test_sync_broker_protective_orders_keeps_retrying_across_ticks_until_it_succeeds():
    @dataclass
    class _FlakyRestingBroker(_RestingBroker):
        fail_times: int = 0

        def place_order(self, order):
            if self.fail_times > 0:
                self.fail_times -= 1
                raise RuntimeError("simulated transient placement failure")
            return super().place_order(order)

    broker = _FlakyRestingBroker(fail_times=2)
    loop, candidate = _armed_candidate_setup(broker)
    position = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=10, avg_entry_price=5.00,
        stop_price=4.50, target_price=None, trailing_stop_pct=None,
        opened_at=datetime.utcnow(), strategy_name="test",
    )
    position.stop_price = 5.00

    loop._sync_broker_protective_orders(candidate, position, _IN_HOURS_NOW)  # tick 1: fails
    assert position.broker_stop_order_id is None
    loop._sync_broker_protective_orders(candidate, position, _IN_HOURS_NOW)  # tick 2: fails
    assert position.broker_stop_order_id is None
    loop._sync_broker_protective_orders(candidate, position, _IN_HOURS_NOW)  # tick 3: succeeds
    assert position.broker_stop_order_id is not None


def test_manage_position_finalizes_via_broker_bracket_without_submitting_its_own_exit():
    broker = _RestingBroker()
    loop, candidate = _armed_candidate_setup(broker)
    from webull_bot.state_machine import transition

    transition(candidate, CandidateState.TRIGGERED)
    transition(candidate, CandidateState.ENTERED)
    transition(candidate, CandidateState.MANAGING)

    position = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=10, avg_entry_price=5.00,
        stop_price=4.50, target_price=None, trailing_stop_pct=None,
        opened_at=datetime.utcnow(), strategy_name="test",
    )
    loop._positions["TEST"] = position
    loop._attach_broker_bracket(candidate, position, _IN_HOURS_NOW)
    stop_id = position.broker_stop_order_id
    broker._resting_orders[stop_id].status = OrderStatus.FILLED
    broker._resting_orders[stop_id].quantity = 10

    snapshot = _snapshot(datetime.utcnow(), 4.40, 4.40, 600_000, 4.39, 4.41, 4.50)
    trades = []
    loop.on_trade_closed = trades.append
    loop._manage_position(candidate, snapshot, snapshot.timestamp)

    assert len(trades) == 1
    assert trades[0].exit_reason == ExitReason.STOP_LOSS
    # No separate software-submitted SELL MARKET order for this exit --
    # _FakeBroker.place_order (via order_manager.submit_signal) was never
    # reached, only the resting stop this test flipped to FILLED directly.
    assert broker._orders == {}


def test_manage_position_cancels_resting_orders_before_a_vwap_failure_exit():
    broker = _RestingBroker(fills_after_polls=1)
    loop, candidate = _armed_candidate_setup(broker)
    loop.position_manager = PositionManager(PositionManagementConfig(
        trailing_stop_pct=None, exit_on_vwap_failure=True, vwap_failure_buffer_pct=0.5,
        time_limit_minutes=None, breakeven_trigger_pct=None,
    ))
    from webull_bot.state_machine import transition

    transition(candidate, CandidateState.TRIGGERED)
    transition(candidate, CandidateState.ENTERED)
    transition(candidate, CandidateState.MANAGING)

    position = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=10, avg_entry_price=5.00,
        stop_price=3.00, target_price=None, trailing_stop_pct=None,  # stop far away, won't fire first
        opened_at=datetime.utcnow(), strategy_name="test",
    )
    loop._positions["TEST"] = position
    loop._attach_broker_bracket(candidate, position, _IN_HOURS_NOW)
    stop_id = position.broker_stop_order_id
    assert stop_id is not None

    snapshot = _snapshot(datetime.utcnow(), 4.90, 5.00, 600_000, 4.89, 4.91, 5.00)  # ~2% below vwap
    loop._manage_position(candidate, snapshot, snapshot.timestamp)

    assert stop_id in broker._cancelled
    assert position.broker_stop_order_id is None
    assert position.broker_target_order_id is None


def test_close_all_positions_now_cancels_resting_orders_before_flattening():
    broker = _RestingBroker(fills_after_polls=1)
    loop, candidate = _armed_candidate_setup(broker)
    from webull_bot.state_machine import transition

    transition(candidate, CandidateState.TRIGGERED)
    transition(candidate, CandidateState.ENTERED)
    transition(candidate, CandidateState.MANAGING)

    position = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=10, avg_entry_price=5.00,
        stop_price=4.50, target_price=None, trailing_stop_pct=None,
        opened_at=datetime.utcnow(), strategy_name="test",
    )
    loop._positions["TEST"] = position
    loop._attach_broker_bracket(candidate, position, _IN_HOURS_NOW)
    stop_id = position.broker_stop_order_id
    assert stop_id is not None

    loop._close_all_positions_now("test", datetime.utcnow())

    assert stop_id in broker._cancelled


def test_reconcile_adopts_a_position_and_attaches_broker_bracket_when_supported():
    broker = _RestingBroker()
    broker._positions.append(Position(
        symbol="TEST", side=OrderSide.BUY, quantity=100, avg_entry_price=5.00, stop_price=None,
        target_price=None, trailing_stop_pct=None, opened_at=datetime.utcnow(), strategy_name="unknown",
    ))
    loop, _ = _armed_candidate_setup(broker)
    loop.candidates.clear()

    loop.reconcile_positions_from_broker(_IN_HOURS_NOW)

    assert "TEST" in loop._positions
    position = loop._positions["TEST"]
    # Adoption now computes a real target_price too (2026-08-11 fix -- see
    # reconcile_positions_from_broker's docstring), so this places a full
    # stop+target OCO bracket, not just a lone stop.
    assert position.broker_stop_order_id is not None
    assert position.broker_target_order_id is not None
    assert len(broker._brackets) == 1


def test_reconcile_cancels_resting_orders_when_dropping_an_externally_closed_position():
    broker = _RestingBroker()
    loop, candidate = _armed_candidate_setup(broker)
    from webull_bot.state_machine import transition

    transition(candidate, CandidateState.TRIGGERED)
    transition(candidate, CandidateState.ENTERED)
    transition(candidate, CandidateState.MANAGING)

    position = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=10, avg_entry_price=5.00,
        stop_price=4.50, target_price=None, trailing_stop_pct=None,
        opened_at=datetime.utcnow(), strategy_name="test",
    )
    loop._positions["TEST"] = position
    loop._attach_broker_bracket(candidate, position, _IN_HOURS_NOW)
    stop_id = position.broker_stop_order_id
    assert stop_id is not None
    # Broker itself has no position for TEST at all (e.g. closed manually in
    # the Webull app) -- broker._positions (the _FakeBroker/get_positions
    # store) stays empty, unlike the resting-order cleanup this asserts.

    loop.reconcile_positions_from_broker(_IN_HOURS_NOW)

    assert "TEST" not in loop._positions
    assert stop_id in broker._cancelled


# -- extra get_positions()-based confirmation for a TRIGGERED entry
# (_maybe_verify_entry_via_positions, called from _poll_pending_entry) ------

def test_poll_pending_entry_waits_for_the_delay_before_checking_positions():
    # fills_after_polls=1000: get_order_status never resolves to FILLED
    # within this test, isolating the position-verification path from the
    # normal order-status-confirms-the-fill path already covered elsewhere.
    broker = _FakeBroker(fills_after_polls=1000)
    loop, candidate = _armed_candidate_setup(broker)
    snapshot = _snapshot(_IN_HOURS_NOW, 5.20, 5.20, 600_000, 5.19, 5.21, 5.15)
    signal = _trigger_and_build_signal(candidate, snapshot)
    loop._submit_entry(candidate, signal, snapshot, snapshot.timestamp)
    assert candidate.symbol in loop._pending_entry_orders

    # _FakeBroker.place_order's BUY branch already appended a position, but
    # only 5s have passed (< the 10s default delay) -- must not self-heal yet.
    loop._poll_pending_entry(candidate, snapshot.timestamp + timedelta(seconds=5))

    assert candidate.symbol in loop._pending_entry_orders
    assert candidate.state.value == "triggered"


def test_poll_pending_entry_self_heals_via_positions_after_delay_when_order_status_still_pending():
    broker = _FakeBroker(fills_after_polls=1000)
    loop, candidate = _armed_candidate_setup(broker)
    snapshot = _snapshot(_IN_HOURS_NOW, 5.20, 5.20, 600_000, 5.19, 5.21, 5.15)
    signal = _trigger_and_build_signal(candidate, snapshot)
    loop._submit_entry(candidate, signal, snapshot, snapshot.timestamp)

    later = snapshot.timestamp + timedelta(seconds=11)
    loop._poll_pending_entry(candidate, later)

    assert candidate.symbol not in loop._pending_entry_orders
    assert candidate.state.value == "managing"
    assert "TEST" in loop._positions
    assert loop._positions["TEST"].avg_entry_price == 5.20  # from the broker's own position


def test_poll_pending_entry_self_heals_via_positions_when_get_order_status_raises():
    class _StatusAlwaysFailsBroker(_FakeBroker):
        def get_order_status(self, broker_order_id):
            raise RuntimeError("simulated get_order_status outage")

    broker = _StatusAlwaysFailsBroker()
    loop, candidate = _armed_candidate_setup(broker)
    snapshot = _snapshot(_IN_HOURS_NOW, 5.20, 5.20, 600_000, 5.19, 5.21, 5.15)
    signal = _trigger_and_build_signal(candidate, snapshot)
    loop._submit_entry(candidate, signal, snapshot, snapshot.timestamp)

    later = snapshot.timestamp + timedelta(seconds=11)
    loop._poll_pending_entry(candidate, later)  # must not raise despite get_order_status failing

    assert candidate.state.value == "managing"
    assert "TEST" in loop._positions


def test_verify_via_positions_runs_at_most_once_per_pending_entry():
    class _CountingBroker(_FakeBroker):
        get_positions_calls: int = 0

        def get_positions(self):
            self.get_positions_calls += 1
            return super().get_positions()

    broker = _CountingBroker(fills_after_polls=1000)
    loop, candidate = _armed_candidate_setup(broker)
    snapshot = _snapshot(_IN_HOURS_NOW, 5.20, 5.20, 600_000, 5.19, 5.21, 5.15)
    signal = _trigger_and_build_signal(candidate, snapshot)
    loop._submit_entry(candidate, signal, snapshot, snapshot.timestamp)
    # Isolate "checked once, not every tick" from the self-heal path covered
    # above -- clear the position _FakeBroker's place_order already
    # appended so the entry stays genuinely unconfirmed at the broker, and
    # reset the counter so only calls from the position-verification check
    # below are counted (submit_entry's own risk-engine open_positions
    # lookup already made one real call of its own).
    broker._positions.clear()
    broker.get_positions_calls = 0

    later = snapshot.timestamp + timedelta(seconds=11)
    loop._poll_pending_entry(candidate, later)
    loop._poll_pending_entry(candidate, later + timedelta(seconds=5))
    loop._poll_pending_entry(candidate, later + timedelta(seconds=10))

    assert broker.get_positions_calls == 1
    assert candidate.symbol in loop._pending_entry_orders  # still genuinely pending
    assert candidate.state.value == "triggered"


def test_entry_position_verify_delay_is_configurable():
    broker = _FakeBroker(fills_after_polls=1000)
    loop, candidate = _armed_candidate_setup(broker)
    loop.config.entry_position_verify_delay_seconds = 3.0
    snapshot = _snapshot(_IN_HOURS_NOW, 5.20, 5.20, 600_000, 5.19, 5.21, 5.15)
    signal = _trigger_and_build_signal(candidate, snapshot)
    loop._submit_entry(candidate, signal, snapshot, snapshot.timestamp)

    loop._poll_pending_entry(candidate, snapshot.timestamp + timedelta(seconds=4))

    assert candidate.state.value == "managing"  # self-healed at the shorter configured delay


def test_verify_via_positions_reverts_to_armed_when_no_signal_on_record():
    broker = _FakeBroker(fills_after_polls=1000)
    loop, candidate = _armed_candidate_setup(broker)
    snapshot = _snapshot(_IN_HOURS_NOW, 5.20, 5.20, 600_000, 5.19, 5.21, 5.15)
    signal = _trigger_and_build_signal(candidate, snapshot)
    loop._submit_entry(candidate, signal, snapshot, snapshot.timestamp)
    loop._entry_signals.pop(candidate.symbol)  # simulate the "shouldn't happen" gap directly

    later = snapshot.timestamp + timedelta(seconds=11)
    loop._poll_pending_entry(candidate, later)

    assert candidate.state.value == "armed"
    assert candidate.symbol not in loop._pending_entry_orders


# -- live-streamed prices for MANAGING/ENTERED positions ---------------------

def test_on_streaming_snapshot_stores_a_live_snapshot():
    broker = _FakeBroker()
    loop, _ = _armed_candidate_setup(broker)
    snapshot = _snapshot(_IN_HOURS_NOW, 5.20, 5.20, 600_000, 5.19, 5.21, 5.15)

    loop._on_streaming_snapshot(snapshot)

    assert loop._get_streaming_snapshot("TEST", _IN_HOURS_NOW) is snapshot


def test_get_streaming_snapshot_returns_none_when_nothing_ever_streamed():
    broker = _FakeBroker()
    loop, _ = _armed_candidate_setup(broker)
    assert loop._get_streaming_snapshot("TEST", _IN_HOURS_NOW) is None


def test_get_streaming_snapshot_returns_none_once_stale(monkeypatch):
    # _on_streaming_snapshot stamps receipt time with the real wall clock
    # (datetime.utcnow()) -- correct in production, where a streamed
    # message's arrival and TradingLoop's own `now` are both real time --
    # but this suite otherwise runs everything off the fixed simulated
    # _IN_HOURS_NOW (see the module comment above it). So the "received
    # at" clock has to be faked here too, or it'd be comparing a real
    # today's-date receipt time against a fixed 2026-08-10 `now`.
    from webull_bot.runtime import trading_loop as trading_loop_module

    fake_now = {"value": _IN_HOURS_NOW}

    class _FakeDateTime(datetime):
        @classmethod
        def utcnow(cls):
            return fake_now["value"]

    monkeypatch.setattr(trading_loop_module, "datetime", _FakeDateTime)

    broker = _FakeBroker()
    loop, _ = _armed_candidate_setup(broker)
    snapshot = _snapshot(_IN_HOURS_NOW, 5.20, 5.20, 600_000, 5.19, 5.21, 5.15)
    loop._on_streaming_snapshot(snapshot)

    fresh_check = _IN_HOURS_NOW + timedelta(seconds=loop.config.streaming_staleness_seconds - 1)
    assert loop._get_streaming_snapshot("TEST", fresh_check) is snapshot

    stale_check = _IN_HOURS_NOW + timedelta(seconds=loop.config.streaming_staleness_seconds + 1)
    assert loop._get_streaming_snapshot("TEST", stale_check) is None


def test_ensure_streaming_subscribed_calls_broker_subscribe_quotes():
    broker = _StreamingBroker()
    loop, _ = _armed_candidate_setup(broker)

    loop._ensure_streaming_subscribed(["TEST"])

    assert broker.subscribe_calls == [["TEST"]]
    assert loop._streaming_supported is True
    assert loop._streaming_requested_symbols == {"TEST"}


def test_ensure_streaming_subscribed_only_sends_new_symbols():
    broker = _StreamingBroker()
    loop, _ = _armed_candidate_setup(broker)

    loop._ensure_streaming_subscribed(["TEST"])
    loop._ensure_streaming_subscribed(["TEST", "OTHER"])

    assert broker.subscribe_calls == [["TEST"], ["OTHER"]]


def test_ensure_streaming_subscribed_is_a_noop_for_already_subscribed_symbols():
    broker = _StreamingBroker()
    loop, _ = _armed_candidate_setup(broker)

    loop._ensure_streaming_subscribed(["TEST"])
    loop._ensure_streaming_subscribed(["TEST"])

    assert broker.subscribe_calls == [["TEST"]]


def test_ensure_streaming_subscribed_permanently_disables_on_not_implemented():
    # _FakeBroker.subscribe_quotes raises NotImplementedError, exactly
    # matching PaperBrokerClient's own deliberate behavior.
    broker = _FakeBroker()
    loop, _ = _armed_candidate_setup(broker)

    loop._ensure_streaming_subscribed(["TEST"])
    assert loop._streaming_supported is False

    # A later call for a different symbol must short-circuit immediately
    # rather than calling subscribe_quotes (and hitting the same
    # NotImplementedError) again.
    calls_before = getattr(broker, "subscribe_calls", None)
    loop._ensure_streaming_subscribed(["OTHER"])
    assert loop._streaming_requested_symbols == set()  # never actually subscribed to anything


def test_ensure_streaming_subscribed_survives_an_unexpected_exception():
    class _BrokenStreamingBroker(_FakeBroker):
        def subscribe_quotes(self, symbols, on_update):
            raise RuntimeError("simulated streaming connection failure")

    broker = _BrokenStreamingBroker()
    loop, _ = _armed_candidate_setup(broker)

    loop._ensure_streaming_subscribed(["TEST"])  # must not raise

    assert loop._streaming_supported is None  # unknown, not permanently disabled -- worth retrying later
    assert loop._streaming_requested_symbols == set()


def test_confirm_entry_filled_subscribes_to_streaming():
    from webull_bot.enums import SignalAction
    from webull_bot.models import Signal
    from webull_bot.state_machine import transition

    broker = _StreamingBroker(fills_after_polls=1)
    loop, candidate = _armed_candidate_setup(broker)
    snapshot = _snapshot(_IN_HOURS_NOW, 5.20, 5.20, 600_000, 5.19, 5.21, 5.15)
    transition(candidate, CandidateState.TRIGGERED)
    signal = Signal(
        symbol="TEST", action=SignalAction.ENTER_LONG, generated_at=snapshot.timestamp,
        strategy_name="test", strategy_version="v1", reference_price=5.20, suggested_stop=5.00,
    )

    loop._submit_entry(candidate, signal, snapshot, snapshot.timestamp)
    loop._poll_pending_entry(candidate, snapshot.timestamp)

    assert broker.subscribe_calls == [["TEST"]]


def test_process_candidate_prefers_a_fresh_streamed_snapshot_for_a_managing_position():
    broker = _StreamingBroker()
    loop, candidate = _armed_candidate_setup(broker)
    from webull_bot.state_machine import transition

    transition(candidate, CandidateState.TRIGGERED)
    transition(candidate, CandidateState.ENTERED)
    transition(candidate, CandidateState.MANAGING)
    position = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=10, avg_entry_price=5.00,
        stop_price=4.50, target_price=None, trailing_stop_pct=None,
        opened_at=_IN_HOURS_NOW, strategy_name="test",
    )
    loop._positions["TEST"] = position

    # REST (_FakeBroker.get_snapshot) always returns last_price=5.20, well
    # above the 4.50 stop -- would NOT trigger an exit. The streamed price
    # below is what should actually be acted on instead.
    streamed = _snapshot(_IN_HOURS_NOW, 4.00, 4.00, 600_000, 3.99, 4.01, 4.50)
    loop._on_streaming_snapshot(streamed)

    loop._process_candidate(candidate, _IN_HOURS_NOW)

    assert candidate.symbol in loop._pending_exit_orders  # exit submitted -- acted on the streamed price


def test_process_all_candidates_excludes_a_fresh_streaming_symbol_from_the_rest_batch():
    broker = _StreamingBroker()
    loop, candidate = _armed_candidate_setup(broker)
    from webull_bot.state_machine import transition

    transition(candidate, CandidateState.TRIGGERED)
    transition(candidate, CandidateState.ENTERED)
    transition(candidate, CandidateState.MANAGING)
    position = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=10, avg_entry_price=5.00,
        stop_price=4.50, target_price=None, trailing_stop_pct=None,
        opened_at=_IN_HOURS_NOW, strategy_name="test",
    )
    loop._positions["TEST"] = position
    broker._positions.append(position)  # reconcile must still see it as a real broker position

    streamed = _snapshot(_IN_HOURS_NOW, 6.00, 6.00, 600_000, 5.99, 6.01, 5.15)
    loop._on_streaming_snapshot(streamed)

    loop._process_all_candidates(_IN_HOURS_NOW)

    assert broker.individual_calls == []  # never fell back to its own get_snapshot() for TEST


# -- live-streamed prices for WATCHING/HEATING_UP/ARMED candidates ----------
# See WebullBrokerClient._merge_streamed_snapshot's docstring for why this
# is safe now (real bid/ask merged in from the QUOTE stream) where it
# wasn't when streaming only covered ENTERED/MANAGING exit checks.

def test_process_candidate_prefers_a_fresh_streamed_snapshot_for_a_watching_candidate():
    broker = _StreamingBroker()
    loop, candidate = _watching_candidate_setup(broker)

    # REST (_FakeBroker.get_snapshot) always returns last_price=5.20 --
    # the streamed price below is what should actually drive scoring.
    streamed = _snapshot(_IN_HOURS_NOW, 7.00, 7.00, 600_000, 6.99, 7.01, 5.15)
    loop._on_streaming_snapshot(streamed)

    loop._process_candidate(candidate, _IN_HOURS_NOW)

    assert candidate.last_price == 7.00  # CandidateWatcher.update stamps this from the snapshot it saw


def test_process_all_candidates_subscribes_every_watch_stage_candidate():
    broker = _StreamingBroker()
    loop, candidate = _watching_candidate_setup(broker)

    loop._process_all_candidates(_IN_HOURS_NOW)

    assert broker.subscribe_calls == [["TEST"]]


def test_process_all_candidates_does_not_subscribe_a_rejected_or_cooldown_candidate():
    from webull_bot.state_machine import transition

    broker = _StreamingBroker()
    loop, candidate = _watching_candidate_setup(broker)
    transition(candidate, CandidateState.REJECTED)

    loop._process_all_candidates(_IN_HOURS_NOW)

    assert broker.subscribe_calls == []


def test_process_all_candidates_excludes_a_fresh_streaming_watch_stage_symbol_from_the_rest_batch():
    broker = _StreamingBroker()
    loop, candidate = _watching_candidate_setup(broker)

    streamed = _snapshot(_IN_HOURS_NOW, 7.00, 7.00, 600_000, 6.99, 7.01, 5.15)
    loop._on_streaming_snapshot(streamed)

    loop._process_all_candidates(_IN_HOURS_NOW)

    assert broker.individual_calls == []  # never fell back to its own get_snapshot() for TEST


def test_process_candidate_falls_back_to_rest_for_a_watching_candidate_with_no_stream_yet():
    broker = _StreamingBroker()
    loop, candidate = _watching_candidate_setup(broker)

    loop._process_candidate(candidate, _IN_HOURS_NOW)

    assert candidate.last_price == 5.20  # _FakeBroker.get_snapshot's fixed REST price -- no stream pushed
    assert broker.individual_calls == ["TEST"]


def test_process_candidate_does_not_use_streaming_for_a_triggered_candidate():
    from webull_bot.enums import SignalAction
    from webull_bot.models import Signal
    from webull_bot.state_machine import transition

    broker = _StreamingBroker(fills_after_polls=5)
    loop, candidate = _armed_candidate_setup(broker)
    snapshot = _snapshot(_IN_HOURS_NOW, 5.20, 5.20, 600_000, 5.19, 5.21, 5.15)
    transition(candidate, CandidateState.TRIGGERED)
    signal = Signal(
        symbol="TEST", action=SignalAction.ENTER_LONG, generated_at=snapshot.timestamp,
        strategy_name="test", strategy_version="v1", reference_price=5.20, suggested_stop=5.00,
    )
    loop._submit_entry(candidate, signal, snapshot, snapshot.timestamp)

    # A streamed update exists, but TRIGGERED must still poll order status
    # over REST, not read the stream -- _STREAMING_ELIGIBLE_STATES excludes it.
    streamed = _snapshot(_IN_HOURS_NOW, 9.00, 9.00, 600_000, 8.99, 9.01, 5.15)
    loop._on_streaming_snapshot(streamed)

    loop._process_candidate(candidate, _IN_HOURS_NOW)

    assert broker.individual_calls == ["TEST"]  # REST get_snapshot was still called


def test_process_all_candidates_retries_a_previously_failed_subscribe():
    # If a candidate's eager first subscribe attempt (at entry-confirm/
    # adoption time) fails transiently, it must not be stuck unstreamed
    # for the rest of the process -- _process_all_candidates' per-tick
    # sweep (over every _STREAMING_ELIGIBLE_STATES symbol, not just
    # watch-stage ones) is what retries it.
    from webull_bot.state_machine import transition

    broker = _StreamingBroker(fail_subscribe_times=1)
    loop, candidate = _armed_candidate_setup(broker)
    transition(candidate, CandidateState.TRIGGERED)
    transition(candidate, CandidateState.ENTERED)
    transition(candidate, CandidateState.MANAGING)
    position = Position(
        symbol="TEST", side=OrderSide.BUY, quantity=10, avg_entry_price=5.00,
        stop_price=4.50, target_price=None, trailing_stop_pct=None,
        opened_at=_IN_HOURS_NOW, strategy_name="test",
    )
    loop._positions["TEST"] = position
    broker._positions.append(position)

    # Simulates the eager attempt at entry-confirm/adoption time failing --
    # _ensure_streaming_subscribed itself swallows the exception (must
    # never let a streaming hiccup crash a tick), but must NOT mark the
    # symbol as subscribed on failure.
    loop._ensure_streaming_subscribed(["TEST"])
    assert loop._streaming_requested_symbols == set()  # not marked as subscribed

    loop._process_all_candidates(_IN_HOURS_NOW)  # per-tick sweep retries it

    assert broker.subscribe_calls == [["TEST"], ["TEST"]]  # failed once, then succeeded
    assert loop._streaming_requested_symbols == {"TEST"}
