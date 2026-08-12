"""
Fully local simulated broker. No external network calls at all -- useful for
unit tests, strategy development, and the early parts of the backtest engine
before real sandbox market data is wired in.

Fills are simulated at the last known snapshot price plus a configurable
slippage/spread assumption; there is no order book.
"""
from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from ...enums import OrderSide, OrderStatus
from ...interfaces.broker import BrokerClient
from ...models import Fill, MarketSnapshot, Order, Position

_order_seq = itertools.count(1)


@dataclass
class PaperBrokerConfig:
    starting_equity: float = 25_000.0
    fill_slippage_bps: float = 5.0   # basis points of adverse slippage applied to market fills
    fee_per_share: float = 0.0


@dataclass
class _PaperState:
    equity: float
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    orders: dict[str, Order] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    latest_snapshots: dict[str, MarketSnapshot] = field(default_factory=dict)


class PaperBrokerClient(BrokerClient):
    def __init__(self, config: Optional[PaperBrokerConfig] = None):
        self.config = config or PaperBrokerConfig()
        self._state = _PaperState(equity=self.config.starting_equity, cash=self.config.starting_equity)
        self._connected = False

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def get_account_equity(self) -> float:
        return self._state.equity

    def get_buying_power(self) -> float:
        return self._state.cash

    def get_positions(self) -> list[Position]:
        return list(self._state.positions.values())

    def feed_snapshot(self, snapshot: MarketSnapshot) -> None:
        """Test/backtest helper: push the latest known price for a symbol."""
        self._state.latest_snapshots[snapshot.symbol] = snapshot

    def get_snapshot(self, symbol: str) -> MarketSnapshot:
        if symbol not in self._state.latest_snapshots:
            raise KeyError(f"No snapshot fed for {symbol} yet")
        return self._state.latest_snapshots[symbol]

    def get_bars(self, symbol: str, interval: str, lookback: int) -> list[MarketSnapshot]:
        raise NotImplementedError("PaperBrokerClient has no historical bar store; feed bars via backtest engine")

    def subscribe_quotes(self, symbols: list[str], on_update: Callable[[MarketSnapshot], None]) -> None:
        raise NotImplementedError("PaperBrokerClient is pull/feed-based; use feed_snapshot() in tests/backtests")

    def _simulated_fill_price(self, order: Order, snapshot: MarketSnapshot) -> float:
        mid = (snapshot.bid + snapshot.ask) / 2 if snapshot.bid and snapshot.ask else snapshot.last_price
        slippage = mid * (self.config.fill_slippage_bps / 10_000)
        if order.side in (OrderSide.BUY, OrderSide.BUY_TO_COVER):
            return mid + slippage
        return mid - slippage

    def place_order(self, order: Order) -> Order:
        if not self._connected:
            raise RuntimeError("PaperBrokerClient.connect() must be called first")
        snapshot = self._state.latest_snapshots.get(order.symbol)
        if snapshot is None:
            order.status = OrderStatus.REJECTED
            return order

        order.broker_order_id = order.broker_order_id or f"paper-{next(_order_seq)}-{uuid.uuid4().hex[:8]}"
        fill_price = self._simulated_fill_price(order, snapshot)
        fees = self.config.fee_per_share * order.quantity

        notional = fill_price * order.quantity
        if order.side == OrderSide.BUY and notional + fees > self._state.cash:
            order.status = OrderStatus.REJECTED
            return order

        fill = Fill(
            order_client_id=order.client_order_id or order.broker_order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=fill_price,
            filled_at=snapshot.timestamp,
            fees=fees,
        )
        self._state.fills.append(fill)
        self._apply_fill(fill)
        order.status = OrderStatus.FILLED
        self._state.orders[order.broker_order_id] = order
        return order

    def _apply_fill(self, fill: Fill) -> None:
        existing = self._state.positions.get(fill.symbol)
        if fill.side in (OrderSide.BUY,):
            self._state.cash -= fill.price * fill.quantity + fill.fees
            if existing is None:
                self._state.positions[fill.symbol] = Position(
                    symbol=fill.symbol,
                    side=OrderSide.BUY,
                    quantity=fill.quantity,
                    avg_entry_price=fill.price,
                    stop_price=None,
                    target_price=None,
                    trailing_stop_pct=None,
                    opened_at=fill.filled_at,
                    strategy_name="unknown",
                )
            else:
                total_qty = existing.quantity + fill.quantity
                existing.avg_entry_price = (
                    existing.avg_entry_price * existing.quantity + fill.price * fill.quantity
                ) / total_qty
                existing.quantity = total_qty
        elif fill.side in (OrderSide.SELL,):
            self._state.cash += fill.price * fill.quantity - fill.fees
            if existing is not None:
                realized = (fill.price - existing.avg_entry_price) * fill.quantity
                existing.realized_pnl += realized
                existing.quantity -= fill.quantity
                if existing.quantity <= 0:
                    del self._state.positions[fill.symbol]
        elif fill.side == OrderSide.SELL_SHORT:
            # Opens/adds to a short: proceeds received now (mirrors BUY's
            # cash outflow in direction, not sign), quantity is share count
            # held short, avg_entry_price is the price shorted at.
            self._state.cash += fill.price * fill.quantity - fill.fees
            if existing is None:
                self._state.positions[fill.symbol] = Position(
                    symbol=fill.symbol,
                    side=OrderSide.SELL_SHORT,
                    quantity=fill.quantity,
                    avg_entry_price=fill.price,
                    stop_price=None,
                    target_price=None,
                    trailing_stop_pct=None,
                    opened_at=fill.filled_at,
                    strategy_name="unknown",
                )
            else:
                total_qty = existing.quantity + fill.quantity
                existing.avg_entry_price = (
                    existing.avg_entry_price * existing.quantity + fill.price * fill.quantity
                ) / total_qty
                existing.quantity = total_qty
        elif fill.side == OrderSide.BUY_TO_COVER:
            # Closes/reduces a short: buying back shares costs cash now: P&L
            # is realized in the OPPOSITE direction from a long's SELL close
            # -- profit comes from price falling below avg_entry_price, not
            # rising above it.
            self._state.cash -= fill.price * fill.quantity + fill.fees
            if existing is not None:
                realized = (existing.avg_entry_price - fill.price) * fill.quantity
                existing.realized_pnl += realized
                existing.quantity -= fill.quantity
                if existing.quantity <= 0:
                    del self._state.positions[fill.symbol]
        # A short position is a liability, not an asset -- its notional
        # contribution to equity is subtracted, not added, unlike a long's.
        self._state.equity = self._state.cash + sum(
            (p.quantity * p.avg_entry_price if p.side != OrderSide.SELL_SHORT else -p.quantity * p.avg_entry_price)
            for p in self._state.positions.values()
        )

    def cancel_order(self, broker_order_id: str) -> None:
        order = self._state.orders.get(broker_order_id)
        if order and order.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.ACCEPTED):
            order.status = OrderStatus.CANCELED

    def modify_order(self, broker_order_id: str, **changes) -> Order:
        order = self._state.orders[broker_order_id]
        for key, value in changes.items():
            setattr(order, key, value)
        return order

    def get_order_status(self, broker_order_id: str) -> Order:
        return self._state.orders[broker_order_id]

    def poll_fills(self, since: Optional[datetime] = None) -> list[Fill]:
        if since is None:
            return list(self._state.fills)
        return [f for f in self._state.fills if f.filled_at >= since]

    @property
    def is_live(self) -> bool:
        return False
