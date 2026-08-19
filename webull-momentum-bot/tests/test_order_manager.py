"""
Tests for OrderManager's broker-side (resting) protective-order methods --
place_resting_stop/place_resting_bracket/cancel_resting_order. These back
TradingLoop's broker-side stop/target bracket feature (see
position_manager.py's module docstring and
WebullBrokerClient.place_oco_bracket's docstring) and are the ONLY sanctioned
way anything outside this class reaches those broker calls (see this
module's own docstring: "the ONLY component in this codebase allowed to
call a BrokerClient's order-placement methods").

submit_signal's ENTRY-sizing behavior (Signal -> RiskEngine.evaluate ->
Order) already has coverage via test_trading_loop.py/test_risk_engine.py;
not duplicated here. What IS covered here: submit_signal's open_positions
requirement for entries specifically -- a real bug fixed 2026-08-11, see
submit_signal's own docstring. Briefly, self.broker.get_positions() used to
be called internally to build RiskEngine.evaluate's open_positions
argument, but every Position a broker returns (WebullBrokerClient or
PaperBrokerClient) hard-codes stop_price=None -- there's no such field in a
broker's raw account-positions response -- so the max_total_risk_pct gate,
which sums assumed risk by filtering on `stop_price is not None`, silently
saw zero risk from every existing position no matter how much was actually
on, and could never reject a new entry on that basis. Now the caller must
pass its own locally-tracked positions (the only ones with a real
stop_price) explicitly.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

import pytest

from webull_bot.config import get_settings
from webull_bot.enums import OrderSide, OrderStatus, OrderType, SignalAction
from webull_bot.execution.order_manager import BracketEntryRejected, OrderManager, OrderRejected
from webull_bot.interfaces.broker import BrokerClient
from webull_bot.models import Fill, MarketSnapshot, Order, Position, Signal
from webull_bot.risk.risk_engine import RiskConfig, RiskEngine


@dataclass
class _NoRestingOrdersBroker(BrokerClient):
    """A broker with no place_oco_bracket at all -- e.g. PaperBrokerClient
    in production, or any broker that hasn't implemented resting orders."""
    _orders: list = field(default_factory=list)

    def connect(self): pass
    def disconnect(self): pass
    def get_account_equity(self): return 25_000.0
    def get_buying_power(self): return 25_000.0
    def get_positions(self): return []
    def get_snapshot(self, symbol): raise NotImplementedError
    def get_bars(self, symbol, interval, lookback): raise NotImplementedError
    def subscribe_quotes(self, symbols, on_update): raise NotImplementedError
    def unsubscribe_quotes(self, symbols): raise NotImplementedError

    def place_order(self, order: Order) -> Order:
        order.broker_order_id = order.client_order_id or "unexpected-call"
        order.status = OrderStatus.SUBMITTED
        self._orders.append(order)
        return order

    def cancel_order(self, broker_order_id: str) -> None: pass
    def modify_order(self, broker_order_id: str, **changes) -> Order: raise NotImplementedError
    def get_order_status(self, broker_order_id: str) -> Order: raise NotImplementedError
    def poll_fills(self, since=None): return []

    @property
    def is_live(self): return False


@dataclass
class _RestingOrdersBroker(_NoRestingOrdersBroker):
    """Adds place_oco_bracket -- the capability OrderManager detects via
    getattr (see _broker_supports_resting_orders) -- so this stands in for
    WebullBrokerClient without needing real network/SDK access."""
    _brackets: list = field(default_factory=list)

    def place_oco_bracket(self, stop_order: Order, target_order: Order):
        stop_order.broker_order_id = "stop-leg-id"
        stop_order.status = OrderStatus.SUBMITTED
        target_order.broker_order_id = "target-leg-id"
        target_order.status = OrderStatus.SUBMITTED
        self._brackets.append((stop_order, target_order))
        return stop_order, target_order


def _order_manager(broker) -> OrderManager:
    return OrderManager(broker, RiskEngine(), get_settings())


# -- capability gate: no place_oco_bracket on the broker -> None, no broker call --

def test_place_resting_stop_returns_none_when_broker_lacks_resting_support():
    broker = _NoRestingOrdersBroker()
    om = _order_manager(broker)
    result = om.place_resting_stop("AAPL", OrderSide.SELL, 100, 9.50)
    assert result is None
    assert broker._orders == []  # broker.place_order was never called


def test_place_resting_bracket_returns_none_when_broker_lacks_resting_support():
    broker = _NoRestingOrdersBroker()
    om = _order_manager(broker)
    result = om.place_resting_bracket("AAPL", OrderSide.SELL, 100, 9.50, 50, 11.00)
    assert result is None


def test_place_resting_trailing_stop_returns_none_when_broker_lacks_resting_support():
    broker = _NoRestingOrdersBroker()
    om = _order_manager(broker)
    result = om.place_resting_trailing_stop("AAPL", OrderSide.SELL, 100, 3.0)
    assert result is None
    assert broker._orders == []  # broker.place_order was never called


# -- broker supports resting orders: build and forward the right Order(s) --

def test_place_resting_stop_builds_a_stop_order_and_submits_it():
    broker = _NoRestingOrdersBroker()  # place_order works, no bracket needed for a lone stop... wait, gated
    # A lone resting stop is still gated on the SAME capability check as the
    # bracket (see _broker_supports_resting_orders' docstring: PaperBrokerClient
    # fills every order synchronously regardless of order_type, so a lone
    # "resting" stop through plain place_order would be wrong there too) --
    # use the broker that actually declares resting-order support.
    broker = _RestingOrdersBroker()
    om = _order_manager(broker)

    order = om.place_resting_stop("AAPL", OrderSide.SELL, 100, 9.50, strategy_name="test", now=datetime(2026, 8, 11, 15, 0, 0))

    assert order is not None
    assert order.status == OrderStatus.SUBMITTED
    assert len(broker._orders) == 1
    submitted = broker._orders[0]
    assert submitted.symbol == "AAPL"
    assert submitted.side == OrderSide.SELL
    assert submitted.order_type == OrderType.STOP
    assert submitted.quantity == 100
    assert submitted.stop_price == 9.50
    assert submitted.strategy_name == "test"


def test_place_resting_trailing_stop_builds_a_trailing_stop_order_and_submits_it():
    broker = _RestingOrdersBroker()
    om = _order_manager(broker)

    order = om.place_resting_trailing_stop(
        "AAPL", OrderSide.SELL, 100, 3.0, strategy_name="test", now=datetime(2026, 8, 11, 15, 0, 0),
    )

    assert order is not None
    assert order.status == OrderStatus.SUBMITTED
    assert len(broker._orders) == 1
    submitted = broker._orders[0]
    assert submitted.symbol == "AAPL"
    assert submitted.side == OrderSide.SELL
    assert submitted.order_type == OrderType.TRAILING_STOP
    assert submitted.quantity == 100
    assert submitted.trailing_pct == 3.0
    assert submitted.stop_price is None
    assert submitted.strategy_name == "test"


def test_place_resting_bracket_builds_stop_and_target_orders_and_submits_the_pair():
    broker = _RestingOrdersBroker()
    om = _order_manager(broker)

    result = om.place_resting_bracket(
        "AAPL", OrderSide.SELL, 100, 9.50, 50, 11.00,
        strategy_name="test", now=datetime(2026, 8, 11, 15, 0, 0),
    )

    assert result is not None
    stop_order, target_order = result
    assert stop_order.broker_order_id == "stop-leg-id"
    assert target_order.broker_order_id == "target-leg-id"
    assert len(broker._brackets) == 1
    sent_stop, sent_target = broker._brackets[0]
    assert sent_stop.order_type == OrderType.STOP
    assert sent_stop.quantity == 100
    assert sent_stop.stop_price == 9.50
    assert sent_target.order_type == OrderType.LIMIT
    assert sent_target.quantity == 50
    assert sent_target.limit_price == 11.00
    assert sent_stop.side == OrderSide.SELL
    assert sent_target.side == OrderSide.SELL


def test_cancel_resting_order_delegates_to_broker_cancel_order():
    calls = []

    @dataclass
    class _TrackingBroker(_RestingOrdersBroker):
        def cancel_order(self, broker_order_id: str) -> None:
            calls.append(broker_order_id)

    broker = _TrackingBroker()
    om = _order_manager(broker)
    om.cancel_resting_order("stop-leg-id")
    assert calls == ["stop-leg-id"]


# -- submit_signal's open_positions requirement for entries (2026-08-11 fix) --

def _entry_signal(**overrides) -> Signal:
    base = dict(
        symbol="ABCD", action=SignalAction.ENTER_LONG, generated_at=datetime(2026, 8, 11, 15, 0, 0),
        strategy_name="test", strategy_version="v1", reference_price=10.0, suggested_stop=9.7,
    )
    base.update(overrides)
    return Signal(**base)


def _entry_snapshot(**overrides) -> MarketSnapshot:
    base = dict(
        symbol="ABCD", timestamp=datetime(2026, 8, 11, 15, 0, 0), last_price=10.0,
        bid=9.99, ask=10.01, bid_size=100, ask_size=100, cumulative_volume=1_000_000,
        vwap=9.8, high_of_day=10.1, low_of_day=9.5, open_price=9.6,
    )
    base.update(overrides)
    return MarketSnapshot(**base)


@dataclass
class _EntryOnlyBroker(_NoRestingOrdersBroker):
    """A broker whose get_positions() raises if it's ever called --
    proves submit_signal's entry path no longer calls it internally (see
    this module's docstring)."""
    def get_positions(self):
        raise AssertionError("submit_signal must not call broker.get_positions() for entry sizing")


def test_submit_signal_requires_open_positions_for_an_entry_signal():
    broker = _EntryOnlyBroker()
    om = OrderManager(broker, RiskEngine(), get_settings())
    with pytest.raises(ValueError, match="open_positions"):
        om.submit_signal(_entry_signal(), snapshot=_entry_snapshot(), now=datetime(2026, 8, 11, 15, 0, 0))


def test_submit_signal_uses_caller_supplied_open_positions_not_the_brokers():
    # A locally-tracked position with a real stop_price, sized so its
    # assumed risk alone already consumes the entire max_total_risk_pct
    # ceiling -- if submit_signal were still calling broker.get_positions()
    # (which raises here, and which would return stop_price=None even if it
    # didn't), this open position's risk would be invisible and the new
    # entry would wrongly be approved.
    broker = _EntryOnlyBroker()  # get_account_equity/get_buying_power both return 25,000
    risk_engine = RiskEngine(RiskConfig(max_total_risk_pct=10.0))  # ceiling: $2,500
    om = OrderManager(broker, risk_engine, get_settings())
    existing_position = Position(
        symbol="EFGH", side=OrderSide.BUY, quantity=1_000, avg_entry_price=5.0,
        stop_price=1.0,  # $4.00/share * 1,000 = $4,000 assumed risk -- well over the $2,500 ceiling
        target_price=None, trailing_stop_pct=None, opened_at=datetime(2026, 8, 11, 14, 0, 0),
        strategy_name="test",
    )

    with pytest.raises(OrderRejected) as exc_info:
        om.submit_signal(
            _entry_signal(), snapshot=_entry_snapshot(), open_positions=[existing_position],
            now=datetime(2026, 8, 11, 15, 0, 0),
        )
    assert "risk" in exc_info.value.decision.reason.lower()


# -- extended-hours order type: marketable LIMIT instead of MARKET (2026-08-12) --
#
# Real incident that motivated this: a resting broker-side OCO stop+target
# bracket (STOP_LOSS+LIMIT legs) was rejected pre-market with
# support_trading_session="ALL" (OAUTH_OPENAPI_PARAM_ERR), even though a
# plain LIMIT order tested clean minutes earlier -- see
# brokers/webull/client.py's _order_payload docstring. MARKET orders were
# never actually confirmed to work outside core hours at all. A marketable
# LIMIT (priced through the current bid/ask by
# OrderManager.EXTENDED_HOURS_LIMIT_BUFFER_PCT) is used instead whenever
# `now` falls outside core hours, for both entries and exits; core-hours
# behavior (plain MARKET) is unchanged.

# 09:00 UTC = 5:00am ET -- pre-market, outside core hours.
_PRE_MARKET_NOW = datetime(2026, 8, 10, 9, 0, 0)


def test_order_type_and_limit_price_returns_market_during_core_hours():
    om = _order_manager(_NoRestingOrdersBroker())
    order_type, limit_price = om._order_type_and_limit_price(
        OrderSide.BUY, _entry_snapshot(), datetime(2026, 8, 11, 15, 0, 0),
    )
    assert order_type == OrderType.MARKET
    assert limit_price is None


def test_order_type_and_limit_price_buy_side_prices_above_the_ask_outside_core_hours():
    om = _order_manager(_NoRestingOrdersBroker())
    order_type, limit_price = om._order_type_and_limit_price(
        OrderSide.BUY, _entry_snapshot(ask=10.00), _PRE_MARKET_NOW,
    )
    assert order_type == OrderType.LIMIT
    assert limit_price == round(10.00 * (1 + om.EXTENDED_HOURS_LIMIT_BUFFER_PCT / 100.0), 2)


def test_order_type_and_limit_price_sell_side_prices_below_the_bid_outside_core_hours():
    om = _order_manager(_NoRestingOrdersBroker())
    order_type, limit_price = om._order_type_and_limit_price(
        OrderSide.SELL, _entry_snapshot(bid=9.98), _PRE_MARKET_NOW,
    )
    assert order_type == OrderType.LIMIT
    assert limit_price == round(9.98 * (1 - om.EXTENDED_HOURS_LIMIT_BUFFER_PCT / 100.0), 2)


def test_order_type_and_limit_price_falls_back_to_last_price_without_a_quote():
    om = _order_manager(_NoRestingOrdersBroker())
    order_type, limit_price = om._order_type_and_limit_price(
        OrderSide.BUY, _entry_snapshot(bid=0.0, ask=0.0, last_price=11.0), _PRE_MARKET_NOW,
    )
    assert order_type == OrderType.LIMIT
    assert limit_price == round(11.0 * (1 + om.EXTENDED_HOURS_LIMIT_BUFFER_PCT / 100.0), 2)


def test_submit_signal_entry_uses_market_during_core_hours():
    broker = _NoRestingOrdersBroker()
    om = _order_manager(broker)
    om.submit_signal(
        _entry_signal(), snapshot=_entry_snapshot(), open_positions=[],
        now=datetime(2026, 8, 11, 15, 0, 0),
    )
    placed = broker._orders[-1]
    assert placed.order_type == OrderType.MARKET
    assert placed.limit_price is None


def test_submit_signal_entry_uses_marketable_limit_outside_core_hours():
    broker = _NoRestingOrdersBroker()
    # allow_extended_hours_trading=True -- otherwise RiskEngine.evaluate's
    # own trading-hours gate rejects this signal before order construction
    # is ever reached; that gate is tested separately in test_risk_engine.py,
    # this test is only about the resulting order's TYPE/PRICE once a
    # signal is approved.
    risk_engine = RiskEngine(RiskConfig(allow_extended_hours_trading=True))
    om = OrderManager(broker, risk_engine, get_settings())
    om.submit_signal(
        _entry_signal(), snapshot=_entry_snapshot(ask=10.00), open_positions=[],
        now=_PRE_MARKET_NOW,
    )
    placed = broker._orders[-1]
    assert placed.order_type == OrderType.LIMIT
    assert placed.limit_price == round(10.00 * (1 + om.EXTENDED_HOURS_LIMIT_BUFFER_PCT / 100.0), 2)


def _open_position(**overrides) -> Position:
    base = dict(
        symbol="ABCD", side=OrderSide.BUY, quantity=100, avg_entry_price=10.0,
        stop_price=9.5, target_price=None, trailing_stop_pct=None,
        opened_at=datetime(2026, 8, 11, 14, 0, 0), strategy_name="test",
    )
    base.update(overrides)
    return Position(**base)


def _exit_signal(**overrides) -> Signal:
    base = dict(
        symbol="ABCD", action=SignalAction.EXIT, generated_at=datetime(2026, 8, 11, 15, 0, 0),
        strategy_name="test", strategy_version="v1", reference_price=10.0, suggested_stop=None,
    )
    base.update(overrides)
    return Signal(**base)


def test_submit_signal_exit_uses_market_during_core_hours():
    broker = _NoRestingOrdersBroker()
    om = _order_manager(broker)
    om.submit_signal(
        _exit_signal(), snapshot=_entry_snapshot(), position=_open_position(),
        now=datetime(2026, 8, 11, 15, 0, 0),
    )
    placed = broker._orders[-1]
    assert placed.order_type == OrderType.MARKET
    assert placed.limit_price is None


def test_submit_signal_exit_uses_marketable_limit_outside_core_hours():
    broker = _NoRestingOrdersBroker()
    om = _order_manager(broker)
    om.submit_signal(
        _exit_signal(), snapshot=_entry_snapshot(bid=9.98), position=_open_position(),
        now=_PRE_MARKET_NOW,
    )
    placed = broker._orders[-1]
    assert placed.order_type == OrderType.LIMIT
    # SELL is the exit side for a long position -- prices below the bid.
    assert placed.limit_price == round(9.98 * (1 - om.EXTENDED_HOURS_LIMIT_BUFFER_PCT / 100.0), 2)


# -- atomic bracket entry (submit_entry_signal, 2026-08-13) ------------------
# See docs/ARCHITECTURE.md's "Atomic bracket entry" section: entries now
# submit their stop/target as part of the SAME broker call as the entry
# itself (when the broker supports it), so a position is never
# broker-managed-less even for an instant during core hours. submit_signal
# itself is deliberately untouched (still used by BacktestEngine/exits) --
# submit_entry_signal is a separate, new method.

@dataclass
class _BracketEntryBroker(_NoRestingOrdersBroker):
    """Adds place_bracket_entry -- the capability OrderManager detects via
    getattr -- so this stands in for WebullBrokerClient without needing
    real network/SDK access. `reject` lets a test simulate the broker
    refusing the combo request."""
    _brackets: list = field(default_factory=list)
    reject: bool = False

    def place_bracket_entry(self, entry_order, stop_order, target_order):
        if self.reject:
            raise RuntimeError("OAUTH_OPENAPI_PARAM_ERR: combo not supported for this instrument")
        entry_order.broker_order_id = "entry-leg-id"
        entry_order.status = OrderStatus.SUBMITTED
        stop_order.broker_order_id = "stop-leg-id"
        stop_order.status = OrderStatus.SUBMITTED
        if target_order is not None:
            target_order.broker_order_id = "target-leg-id"
            target_order.status = OrderStatus.SUBMITTED
        self._brackets.append((entry_order, stop_order, target_order))
        return entry_order, stop_order, target_order


def test_submit_entry_signal_raises_order_rejected_before_any_broker_call():
    broker = _EntryOnlyBroker()  # get_positions raises -- must never be reached
    risk_engine = RiskEngine(RiskConfig(max_total_risk_pct=10.0))
    om = OrderManager(broker, risk_engine, get_settings())
    existing_position = Position(
        symbol="EFGH", side=OrderSide.BUY, quantity=1_000, avg_entry_price=5.0,
        stop_price=1.0, target_price=None, trailing_stop_pct=None,
        opened_at=datetime(2026, 8, 11, 14, 0, 0), strategy_name="test",
    )
    with pytest.raises(OrderRejected):
        om.submit_entry_signal(
            _entry_signal(), snapshot=_entry_snapshot(), open_positions=[existing_position],
            now=datetime(2026, 8, 11, 15, 0, 0),
        )


def test_submit_entry_signal_falls_back_to_plain_entry_without_broker_capability():
    # No place_bracket_entry on this broker at all -- lacking the capability
    # is NOT a rejection, just falls back to a plain unbracketed entry.
    broker = _NoRestingOrdersBroker()
    om = _order_manager(broker)
    result = om.submit_entry_signal(
        _entry_signal(suggested_target=11.0), snapshot=_entry_snapshot(), open_positions=[],
        now=datetime(2026, 8, 11, 15, 0, 0),
    )
    assert result.stop_order is None
    assert result.target_order is None
    assert result.entry_order.status == OrderStatus.SUBMITTED
    assert broker._orders == [result.entry_order]


def test_submit_entry_signal_falls_back_when_signal_has_no_suggested_target():
    # Broker DOES support atomic brackets, but the signal itself has no
    # suggested_target (default _entry_signal()) -- still falls back, same
    # non-rejection reasoning as the capability-gate test above.
    broker = _BracketEntryBroker()
    om = _order_manager(broker)
    result = om.submit_entry_signal(
        _entry_signal(), snapshot=_entry_snapshot(), open_positions=[],
        now=datetime(2026, 8, 11, 15, 0, 0),
    )
    assert result.stop_order is None
    assert result.target_order is None
    assert broker._brackets == []


def test_submit_entry_signal_falls_back_to_plain_entry_outside_core_hours():
    """rtms-v3-follow-up (2026-08-19, real incident: BTCT's atomic bracket
    was rejected -- HTTP 417 OAUTH_OPENAPI_PARAM_ERR, "invalid
    support_trading_session, value: ALL" -- meaning the trade never went
    through at all, since submit_entry_signal's contract on a genuine
    broker rejection is "no fallback"). Broker DOES support atomic
    brackets and the signal DOES have both suggested_stop/suggested_target
    (unlike the two fallback tests above) -- the only thing forcing the
    fallback here is `now` being outside core trading hours. Extended-
    hours counterpart to test_submit_entry_signal_uses_atomic_bracket_when_supported,
    which already covers this exact scenario during core hours and proves
    the atomic path there is untouched."""
    broker = _BracketEntryBroker()
    # allow_extended_hours_trading=True -- otherwise RiskEngine.evaluate's
    # own trading-hours gate rejects this signal before it ever reaches
    # submit_entry_signal's atomic-vs-plain decision; see
    # test_submit_signal_entry_uses_marketable_limit_outside_core_hours'
    # own note above for the same reasoning.
    risk_engine = RiskEngine(RiskConfig(allow_extended_hours_trading=True))
    om = OrderManager(broker, risk_engine, get_settings())
    result = om.submit_entry_signal(
        _entry_signal(suggested_target=11.0), snapshot=_entry_snapshot(), open_positions=[],
        now=_PRE_MARKET_NOW,
    )
    assert result.stop_order is None
    assert result.target_order is None
    assert broker._brackets == []  # place_bracket_entry never called
    assert broker._orders == [result.entry_order]
    # _order_type_and_limit_price's existing extended-hours LIMIT pricing
    # still applies to the plain entry -- unaffected by this change.
    assert result.entry_order.order_type == OrderType.LIMIT


def test_submit_entry_signal_uses_atomic_bracket_when_supported():
    broker = _BracketEntryBroker()
    risk_engine = RiskEngine(RiskConfig(max_position_size_pct=100.0))
    om = OrderManager(broker, risk_engine, get_settings())
    result = om.submit_entry_signal(
        _entry_signal(suggested_target=11.0), snapshot=_entry_snapshot(), open_positions=[],
        now=datetime(2026, 8, 11, 15, 0, 0),
    )
    assert result.entry_order.status == OrderStatus.SUBMITTED
    assert result.stop_order is not None
    assert result.stop_order.side == OrderSide.SELL
    assert result.stop_order.order_type == OrderType.STOP
    assert result.stop_order.stop_price == 9.7
    # Stop protects the FULL entry quantity.
    assert result.stop_order.quantity == result.entry_order.quantity
    assert result.target_order is not None
    assert result.target_order.side == OrderSide.SELL
    assert result.target_order.order_type == OrderType.LIMIT
    assert result.target_order.limit_price == 11.0
    # Target leg is a partial exit -- HALF the entry quantity (mirrors
    # TradingLoop._attach_broker_bracket's own halving rule).
    assert result.target_order.quantity == int(result.entry_order.quantity // 2)
    assert len(broker._brackets) == 1


def test_submit_entry_signal_omits_target_leg_when_quantity_too_small_to_split():
    broker = _BracketEntryBroker()
    # A tiny position size ceiling forces decision.max_shares down to 1
    # share, whose half (0) is too small to give the target leg any real
    # quantity at all.
    risk_engine = RiskEngine(RiskConfig(max_position_size_pct=0.05))
    om = OrderManager(broker, risk_engine, get_settings())
    result = om.submit_entry_signal(
        _entry_signal(suggested_target=11.0), snapshot=_entry_snapshot(), open_positions=[],
        now=datetime(2026, 8, 11, 15, 0, 0),
    )
    assert result.entry_order.quantity == 1
    assert result.stop_order is not None  # stop still protects the full (1-share) position
    assert result.target_order is None


def test_submit_entry_signal_raises_bracket_entry_rejected_on_broker_rejection():
    broker = _BracketEntryBroker(reject=True)
    om = _order_manager(broker)
    with pytest.raises(BracketEntryRejected, match="combo not supported"):
        om.submit_entry_signal(
            _entry_signal(suggested_target=11.0), snapshot=_entry_snapshot(), open_positions=[],
            now=datetime(2026, 8, 11, 15, 0, 0),
        )
    # No fallback attempted -- place_order (the plain entry path) was never
    # called on this broker at all.
    assert broker._orders == []
