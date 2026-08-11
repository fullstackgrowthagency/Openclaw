"""
Tests for OrderManager's broker-side (resting) protective-order methods --
place_resting_stop/place_resting_bracket/cancel_resting_order. These back
TradingLoop's broker-side stop/target bracket feature (see
position_manager.py's module docstring and
WebullBrokerClient.place_oco_bracket's docstring) and are the ONLY sanctioned
way anything outside this class reaches those broker calls (see this
module's own docstring: "the ONLY component in this codebase allowed to
call a BrokerClient's order-placement methods").

submit_signal (the pre-existing Signal-driven entry/exit path) already has
coverage via test_trading_loop.py/test_risk_engine.py; not duplicated here.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from webull_bot.config import get_settings
from webull_bot.enums import OrderSide, OrderStatus, OrderType
from webull_bot.execution.order_manager import OrderManager
from webull_bot.interfaces.broker import BrokerClient
from webull_bot.models import Fill, MarketSnapshot, Order, Position
from webull_bot.risk.risk_engine import RiskEngine


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
