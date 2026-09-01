"""
In-memory paper broker -- the backend behind both backtesting and any
future "paper trading" mode, mirroring webull-momentum-bot's
PaperBrokerClient's role. Fills MARKET orders synchronously by crossing
the spread (buy at ask, sell at bid) plus an optional configurable
slippage in pips, against whatever snapshot was last fed via
`feed_snapshot` -- there is no real network/broker round-trip here.

Deliberately minimal for this Phase 2 skeleton: only MARKET orders are
supported (LIMIT/STOP order simulation isn't built yet), and every
position close is recorded as ExitReason.MANUAL -- there is no automatic
stop-loss/target-triggering machinery yet (that's position management, a
later phase), so every close today genuinely IS a strategy-driven
decision, not an automatic one. Revisit this once that exists; a real
Order-level exit-reason field will be needed then to distinguish them.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from ...enums import ExitReason, OrderSide, OrderStatus, OrderType
from ...interfaces.broker import BrokerClient
from ...models import Fill, MarketSnapshot, Order, Position, Trade
from ...pairs import pips_to_price_diff


class PaperBrokerClient(BrokerClient):
    def __init__(self, *, initial_equity: float = 10_000.0, slippage_pips: float = 0.0):
        self._connected = False
        self._equity = initial_equity
        self._slippage_pips = slippage_pips
        self._positions: dict[str, Position] = {}
        self._latest_snapshot: dict[str, MarketSnapshot] = {}
        self._history: dict[str, list[MarketSnapshot]] = {}
        self._orders: dict[str, Order] = {}
        self._fills: list[Fill] = []
        self.trades: list[Trade] = []
        self._next_order_id = 0

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def get_account_equity(self) -> float:
        return self._equity

    def get_free_margin(self) -> float:
        # Simplification for this skeleton: no margin/leverage modeling
        # yet, so free margin is just equity. Revisit once real margin
        # checks matter for risk validation.
        return self._equity

    def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    def feed_snapshot(self, snapshot: MarketSnapshot) -> None:
        """Drives both get_snapshot and get_bars -- the backtest engine
        (and any live paper-trading loop) calls this once per tick/bar
        before asking a strategy to evaluate it."""
        self._latest_snapshot[snapshot.symbol] = snapshot
        self._history.setdefault(snapshot.symbol, []).append(snapshot)

    def get_snapshot(self, symbol: str) -> MarketSnapshot:
        if symbol not in self._latest_snapshot:
            raise KeyError(f"No snapshot fed yet for {symbol!r} -- call feed_snapshot first.")
        return self._latest_snapshot[symbol]

    def get_bars(self, symbol: str, interval: str, lookback: int) -> list[MarketSnapshot]:
        # `interval` is currently unused -- this paper broker doesn't yet
        # distinguish timeframes, it just returns whatever was fed.
        return self._history.get(symbol, [])[-lookback:]

    def subscribe_quotes(self, symbols: list[str], on_update: Callable[[MarketSnapshot], None]) -> None:
        raise NotImplementedError("PaperBrokerClient has no real streaming -- feed snapshots via feed_snapshot.")

    def unsubscribe_quotes(self, symbols: list[str]) -> None:
        raise NotImplementedError("PaperBrokerClient has no real streaming -- feed snapshots via feed_snapshot.")

    def place_order(self, order: Order) -> Order:
        if order.order_type != OrderType.MARKET:
            raise NotImplementedError(f"PaperBrokerClient only fills MARKET orders so far, got {order.order_type}.")

        snapshot = self.get_snapshot(order.symbol)
        slippage = pips_to_price_diff(order.symbol, self._slippage_pips)
        fill_price = snapshot.ask + slippage if order.side == OrderSide.BUY else snapshot.bid - slippage

        self._next_order_id += 1
        order.broker_order_id = f"paper-{self._next_order_id}"
        order.status = OrderStatus.FILLED
        # Simulated time (the snapshot's own timestamp), NOT wall-clock
        # datetime.utcnow() -- this fill happens "at" whatever moment the
        # backtest/paper-trading loop is currently replaying, which matters
        # for both live paper trading (fine either way) and, critically,
        # backtesting historical data: a 2026-01-01 bar must produce a
        # 2026-01-01 trade record, not "whenever this process happened to
        # run in real time."
        order.updated_at = snapshot.timestamp
        self._orders[order.broker_order_id] = order

        self._fills.append(Fill(
            order_client_id=order.client_order_id or order.broker_order_id, symbol=order.symbol,
            side=order.side, quantity=order.quantity, price=fill_price, filled_at=order.updated_at,
        ))

        existing = self._positions.get(order.symbol)
        if existing is None:
            self._positions[order.symbol] = Position(
                symbol=order.symbol, side=order.side, quantity=order.quantity, avg_entry_price=fill_price,
                stop_price=order.stop_loss_price, target_price=order.take_profit_price,
                trailing_stop_pips=None, opened_at=order.updated_at, strategy_name=order.strategy_name or "",
            )
        elif existing.side != order.side and existing.quantity == order.quantity:
            self._close_position(existing, fill_price, order.updated_at, order.exit_reason or ExitReason.MANUAL)
        else:
            # Same-side add (pyramiding) or a partial close -- not
            # supported yet; OrderManager doesn't submit either of these
            # in this skeleton (max_simultaneous_positions defaults to 1
            # and exits always close the full quantity), so this should
            # be unreachable through the current wiring.
            raise NotImplementedError(
                "PaperBrokerClient doesn't yet support adding to or partially closing an existing position."
            )

        return order

    def _close_position(
        self, position: Position, exit_price: float, closed_at: datetime, exit_reason: ExitReason,
    ) -> None:
        del self._positions[position.symbol]
        direction = 1.0 if position.side == OrderSide.BUY else -1.0
        pnl = (exit_price - position.avg_entry_price) * position.quantity * direction
        notional = position.avg_entry_price * position.quantity
        self._equity += pnl
        self.trades.append(Trade(
            symbol=position.symbol, strategy_name=position.strategy_name, side=position.side,
            entry_price=position.avg_entry_price, exit_price=exit_price, quantity=position.quantity,
            opened_at=position.opened_at, closed_at=closed_at, exit_reason=exit_reason,
            pnl=pnl, pnl_pct=(pnl / notional) if notional else 0.0,
            max_favorable_excursion=0.0, max_adverse_excursion=0.0,
        ))

    def cancel_order(self, broker_order_id: str) -> None:
        pass  # every order fills synchronously above -- nothing is ever left pending to cancel

    def modify_order(self, broker_order_id: str, **changes) -> Order:
        raise NotImplementedError("Nothing is ever pending in PaperBrokerClient -- there's no order to modify.")

    def get_order_status(self, broker_order_id: str) -> Order:
        if broker_order_id not in self._orders:
            raise KeyError(f"Unknown broker_order_id: {broker_order_id!r}")
        return self._orders[broker_order_id]

    def poll_fills(self, since: Optional[datetime] = None) -> list[Fill]:
        if since is None:
            return list(self._fills)
        return [f for f in self._fills if f.filled_at >= since]

    @property
    def is_live(self) -> bool:
        return False
