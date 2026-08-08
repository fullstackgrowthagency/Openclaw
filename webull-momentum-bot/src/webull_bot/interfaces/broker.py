"""
Broker abstraction. Every execution path (paper, sandbox, live) implements
this same interface so strategies, the risk engine, and the order manager
never need to know which backend they're talking to.

IMPORTANT: nothing outside `execution/order_manager.py` should call these
methods directly. Strategies emit Signals; the risk engine approves or
rejects them; only the OrderManager is wired to a BrokerClient.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Callable, Optional

from ..models import Fill, MarketSnapshot, Order, Position


class BrokerClient(ABC):
    """Abstract broker/execution interface."""

    @abstractmethod
    def connect(self) -> None:
        ...

    @abstractmethod
    def disconnect(self) -> None:
        ...

    @abstractmethod
    def get_account_equity(self) -> float:
        ...

    @abstractmethod
    def get_buying_power(self) -> float:
        ...

    @abstractmethod
    def get_positions(self) -> list[Position]:
        ...

    @abstractmethod
    def get_snapshot(self, symbol: str) -> MarketSnapshot:
        ...

    @abstractmethod
    def get_bars(self, symbol: str, interval: str, lookback: int) -> list[MarketSnapshot]:
        """Historical/recent bars, most-recent last. interval e.g. '1m'."""
        ...

    @abstractmethod
    def subscribe_quotes(self, symbols: list[str], on_update: Callable[[MarketSnapshot], None]) -> None:
        """Stream real-time quotes/bars. Prefer this over polling wherever the backend supports it."""
        ...

    @abstractmethod
    def place_order(self, order: Order) -> Order:
        """Submit an order; returns the order updated with broker_order_id/status."""
        ...

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> None:
        ...

    @abstractmethod
    def modify_order(self, broker_order_id: str, **changes) -> Order:
        ...

    @abstractmethod
    def get_order_status(self, broker_order_id: str) -> Order:
        ...

    @abstractmethod
    def poll_fills(self, since: Optional[datetime] = None) -> list[Fill]:
        ...

    @property
    @abstractmethod
    def is_live(self) -> bool:
        """True only for a backend that can move real money."""
        ...
