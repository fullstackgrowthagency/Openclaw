from datetime import datetime
from typing import Callable, Optional

from fx_bot.brokers.paper.client import PaperBrokerClient
from fx_bot.enums import OrderSide, OrderStatus, OrderType, SignalAction
from fx_bot.execution.order_manager import OrderManager
from fx_bot.interfaces.broker import BrokerClient
from fx_bot.models import Fill, MarketSnapshot, Order, Position, Signal
from fx_bot.risk.risk_engine import RiskConfig, RiskEngine


class _RecordingBroker(BrokerClient):
    """Just records every Order passed to place_order and echoes it back
    ACCEPTED -- enough to test OrderManager's own routing logic in
    isolation, before PaperBrokerClient's fill simulation exists."""

    def __init__(self):
        self.placed_orders: list[Order] = []

    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def get_account_equity(self) -> float: return 10_000.0
    def get_free_margin(self) -> float: return 10_000.0
    def get_positions(self) -> list[Position]: return []
    def get_snapshot(self, symbol: str) -> MarketSnapshot:
        return MarketSnapshot(symbol=symbol, timestamp=datetime.utcnow(), bid=1.0, ask=1.0002)
    def get_bars(self, symbol: str, interval: str, lookback: int) -> list[MarketSnapshot]: return []
    def subscribe_quotes(self, symbols: list[str], on_update: Callable[[MarketSnapshot], None]) -> None: ...
    def unsubscribe_quotes(self, symbols: list[str]) -> None: ...

    def place_order(self, order: Order) -> Order:
        order.status = OrderStatus.ACCEPTED
        order.broker_order_id = f"fake-{len(self.placed_orders)}"
        self.placed_orders.append(order)
        return order

    def cancel_order(self, broker_order_id: str) -> None: ...
    def modify_order(self, broker_order_id: str, **changes) -> Order:
        raise NotImplementedError
    def get_order_status(self, broker_order_id: str) -> Order:
        raise NotImplementedError
    def poll_fills(self, since: Optional[datetime] = None) -> list[Fill]: return []

    @property
    def is_live(self) -> bool: return False


def _snapshot() -> MarketSnapshot:
    return MarketSnapshot(symbol="EUR/USD", timestamp=datetime.utcnow(), bid=1.0999, ask=1.1001)


def _position(symbol="EUR/USD", side=OrderSide.BUY) -> Position:
    return Position(
        symbol=symbol, side=side, quantity=10_000, avg_entry_price=1.1000,
        stop_price=1.0980, target_price=1.1040, trailing_stop_pips=None,
        opened_at=datetime.utcnow(), strategy_name="test",
    )


def test_submit_entry_places_a_market_order_with_the_signals_bracket():
    broker = _RecordingBroker()
    manager = OrderManager(broker, RiskEngine())
    signal = Signal(
        symbol="EUR/USD", action=SignalAction.ENTER_LONG, generated_at=datetime.utcnow(),
        strategy_name="test", strategy_version="v1", reference_price=1.1000,
        suggested_stop=1.0980, suggested_target=1.1040,
    )

    order = manager.submit_signal(signal, snapshot=_snapshot(), open_positions=[])

    assert order is not None
    assert order.side == OrderSide.BUY
    assert order.order_type == OrderType.MARKET
    assert order.stop_loss_price == 1.0980
    assert order.take_profit_price == 1.1040
    assert broker.placed_orders == [order]


def test_submit_entry_returns_none_when_risk_engine_rejects():
    broker = _RecordingBroker()
    manager = OrderManager(broker, RiskEngine(RiskConfig(max_simultaneous_positions=0)))
    signal = Signal(
        symbol="EUR/USD", action=SignalAction.ENTER_LONG, generated_at=datetime.utcnow(),
        strategy_name="test", strategy_version="v1", reference_price=1.1000, suggested_stop=1.0980,
    )

    order = manager.submit_signal(signal, snapshot=_snapshot(), open_positions=[])

    assert order is None
    assert broker.placed_orders == []


def test_exit_is_never_gated_by_the_risk_engine():
    broker = _RecordingBroker()
    # A RiskConfig that would reject every possible entry -- proves the
    # exit path doesn't route through evaluate() at all, not just that
    # evaluate() happens to approve this particular exit.
    manager = OrderManager(broker, RiskEngine(RiskConfig(max_simultaneous_positions=0)))
    position = _position(side=OrderSide.BUY)
    signal = Signal(
        symbol="EUR/USD", action=SignalAction.EXIT, generated_at=datetime.utcnow(),
        strategy_name="test", strategy_version="v1", reference_price=1.1040,
    )

    order = manager.submit_signal(signal, snapshot=_snapshot(), open_positions=[position])

    assert order is not None
    assert order.side == OrderSide.SELL  # closes a BUY position
    assert order.quantity == position.quantity


def test_exit_with_no_matching_open_position_is_a_no_op():
    broker = _RecordingBroker()
    manager = OrderManager(broker, RiskEngine())
    signal = Signal(
        symbol="EUR/USD", action=SignalAction.EXIT, generated_at=datetime.utcnow(),
        strategy_name="test", strategy_version="v1", reference_price=1.1040,
    )

    order = manager.submit_signal(signal, snapshot=_snapshot(), open_positions=[])

    assert order is None
    assert broker.placed_orders == []


def test_scale_in_and_scale_out_are_not_yet_implemented():
    broker = _RecordingBroker()
    manager = OrderManager(broker, RiskEngine())
    for action in (SignalAction.SCALE_IN, SignalAction.SCALE_OUT):
        signal = Signal(
            symbol="EUR/USD", action=action, generated_at=datetime.utcnow(),
            strategy_name="test", strategy_version="v1", reference_price=1.1000,
        )
        assert manager.submit_signal(signal, snapshot=_snapshot(), open_positions=[]) is None
    assert broker.placed_orders == []


def test_exit_feeds_the_realized_pnl_back_to_the_risk_engine():
    # Uses PaperBrokerClient (not _RecordingBroker) because this needs a
    # real Fill to match against -- _record_realized_pnl looks one up via
    # poll_fills(), which _RecordingBroker always returns empty.
    broker = PaperBrokerClient(initial_equity=10_000.0)
    risk_engine = RiskEngine()
    manager = OrderManager(broker, risk_engine)

    entry_time = datetime(2026, 1, 1, 0, 0, 0)
    broker.feed_snapshot(MarketSnapshot(symbol="EUR/USD", timestamp=entry_time, bid=1.0999, ask=1.1001))
    broker.place_order(Order(symbol="EUR/USD", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=10_000))
    position = broker.get_positions()[0]

    exit_time = datetime(2026, 1, 1, 0, 5, 0)
    broker.feed_snapshot(MarketSnapshot(symbol="EUR/USD", timestamp=exit_time, bid=1.0949, ask=1.0951))  # a loss
    signal = Signal(
        symbol="EUR/USD", action=SignalAction.EXIT, generated_at=exit_time,
        strategy_name="test", strategy_version="v1", reference_price=1.0950,
    )

    manager.submit_signal(signal, snapshot=broker.get_snapshot("EUR/USD"), open_positions=[position])

    assert risk_engine._daily.realized_pnl < 0  # the loss was recorded...
    assert "EUR/USD" in risk_engine._last_loss_at  # ...and triggered the cooldown tracker
    assert risk_engine._last_loss_at["EUR/USD"] == exit_time
