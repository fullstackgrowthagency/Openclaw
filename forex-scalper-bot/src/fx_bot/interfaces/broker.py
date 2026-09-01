"""
Broker/connector abstraction. Mirrors webull-momentum-bot/src/webull_bot/
interfaces/broker.py's contract: every execution backend (paper, the local
MT4/5 connector, any future bridge) implements this same interface so
strategies, the risk engine, and the order manager never need to know which
backend they're talking to.

IMPORTANT: nothing outside the execution layer should call these methods
directly -- strategies emit Signals; the risk engine approves or rejects
them; only the order manager is wired to a BrokerClient. Same discipline as
the equities bot, restated here since this ABC is the enforcement point.

Terminology note: `get_free_margin` (not `get_buying_power`) matches how
MT4/5 and forex brokers generally describe available margin -- both
concepts answer "how much more exposure can this account take on," but
`free_margin` is the term this domain actually uses.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Callable, Optional

from ..models import Fill, MarketSnapshot, Order, Position


class BrokerClient(ABC):
    """Abstract broker/connector execution interface."""

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
    def get_free_margin(self) -> float:
        ...

    @abstractmethod
    def get_positions(self) -> list[Position]:
        ...

    @abstractmethod
    def get_snapshot(self, symbol: str) -> MarketSnapshot:
        ...

    @abstractmethod
    def get_bars(self, symbol: str, interval: str, lookback: int) -> list[MarketSnapshot]:
        """Historical/recent bars, most-recent last. interval e.g. '1m'.
        Exact bar shape (bid/ask snapshots vs. true OHLC) is finalized
        once the indicators/backtest phase lands -- see that phase's
        design before assuming this returns OHLC data as-is."""
        ...

    @abstractmethod
    def subscribe_quotes(self, symbols: list[str], on_update: Callable[[MarketSnapshot], None]) -> None:
        """Stream real-time quotes. Prefer this over polling wherever the
        backend supports it."""
        ...

    @abstractmethod
    def unsubscribe_quotes(self, symbols: list[str]) -> None:
        """Stop streaming quotes for `symbols` previously passed to
        subscribe_quotes. Required (not optional/getattr-gated), same
        reasoning as subscribe_quotes: a backend without real streaming
        support should raise NotImplementedError rather than silently
        doing nothing."""
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
