from datetime import datetime
from typing import Callable, Optional

import pytest

from fx_bot.enums import OrderSide, OrderType, SignalAction
from fx_bot.interfaces.broker import BrokerClient
from fx_bot.interfaces.strategy import Strategy
from fx_bot.models import Fill, MarketSnapshot, Order, Position, Signal


def test_strategy_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        Strategy()


def test_broker_client_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        BrokerClient()


class _AlwaysLongStrategy(Strategy):
    name = "always_long"
    version = "v1"

    def on_snapshot(self, symbol, snapshot, history):
        return Signal(
            symbol=symbol, action=SignalAction.ENTER_LONG, generated_at=snapshot.timestamp,
            strategy_name=self.name, strategy_version=self.version, reference_price=snapshot.mid,
        )


def test_a_concrete_strategy_emits_a_signal():
    strategy = _AlwaysLongStrategy()
    snapshot = MarketSnapshot(symbol="EUR/USD", timestamp=datetime.utcnow(), bid=1.1000, ask=1.1002)
    signal = strategy.on_snapshot("EUR/USD", snapshot, history=[])
    assert signal.action == SignalAction.ENTER_LONG
    assert signal.symbol == "EUR/USD"


class _NullBroker(BrokerClient):
    """Minimal concrete BrokerClient, just enough to prove the ABC's method
    set is complete and implementable -- not a real backend."""

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
    def place_order(self, order: Order) -> Order: return order
    def cancel_order(self, broker_order_id: str) -> None: ...
    def modify_order(self, broker_order_id: str, **changes) -> Order:
        return Order(symbol="EUR/USD", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=1)
    def get_order_status(self, broker_order_id: str) -> Order:
        return Order(symbol="EUR/USD", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=1)
    def poll_fills(self, since: Optional[datetime] = None) -> list[Fill]: return []

    @property
    def is_live(self) -> bool: return False


def test_a_concrete_broker_client_reports_its_own_liveness():
    broker = _NullBroker()
    assert broker.is_live is False
    assert broker.get_account_equity() == 10_000.0
