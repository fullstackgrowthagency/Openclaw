"""
Order manager -- the only module wired to a BrokerClient (see that ABC's
own docstring). Converts an approved Signal into an Order and submits it.
Mirrors webull-momentum-bot's execution/order_manager.py in role.
SCALE_IN/SCALE_OUT (partial exits) aren't implemented yet, same deferral
the equities bot itself made (that project's own partial-exit support
landed well after its initial skeleton, once PositionManager existed to
drive it -- see position/position_manager.py, added in this same phase,
which still doesn't do partial exits either).
"""
from __future__ import annotations

from typing import Optional

from ..enums import ExitReason, OrderSide, OrderType, SignalAction
from ..interfaces.broker import BrokerClient
from ..models import MarketSnapshot, Order, Position, Signal
from ..risk.risk_engine import RiskEngine


class OrderManager:
    def __init__(self, broker: BrokerClient, risk_engine: RiskEngine):
        self.broker = broker
        self.risk_engine = risk_engine

    def submit_signal(
        self, signal: Signal, *, snapshot: MarketSnapshot, open_positions: list[Position],
    ) -> Optional[Order]:
        if signal.action in (SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT):
            return self._submit_entry(signal, snapshot=snapshot, open_positions=open_positions)
        if signal.action == SignalAction.EXIT:
            return self._submit_exit(signal, open_positions=open_positions)
        # SCALE_IN/SCALE_OUT: not implemented in this skeleton -- see docstring.
        return None

    def _submit_entry(
        self, signal: Signal, *, snapshot: MarketSnapshot, open_positions: list[Position],
    ) -> Optional[Order]:
        decision = self.risk_engine.evaluate(
            signal, account_equity=self.broker.get_account_equity(),
            open_positions=open_positions, snapshot=snapshot, now=signal.generated_at,
        )
        if not decision.approved:
            return None
        side = OrderSide.BUY if signal.action == SignalAction.ENTER_LONG else OrderSide.SELL
        order = Order(
            symbol=signal.symbol, side=side, order_type=OrderType.MARKET, quantity=decision.max_units,
            stop_loss_price=signal.suggested_stop, take_profit_price=signal.suggested_target,
            strategy_name=signal.strategy_name, signal_id=signal.metadata.get("signal_id"),
            # Simulated time (see Signal.generated_at, always set from the
            # triggering snapshot's own timestamp), not the wall-clock
            # default Order.created_at would otherwise get -- matters for
            # the same reason PaperBrokerClient's fill timestamp does.
            created_at=signal.generated_at, updated_at=signal.generated_at,
        )
        return self.broker.place_order(order)

    def _submit_exit(self, signal: Signal, *, open_positions: list[Position]) -> Optional[Order]:
        position = next((p for p in open_positions if p.symbol == signal.symbol), None)
        if position is None:
            return None  # nothing open for this symbol -- a stale/duplicate exit signal, not an error
        closing_side = OrderSide.SELL if position.side == OrderSide.BUY else OrderSide.BUY
        # Position management (see backtest/engine.py) tags its own
        # auto-triggered closes via this metadata key so the resulting
        # Trade records STOP_LOSS/PROFIT_TARGET accurately -- a plain
        # strategy-driven EXIT signal (no such key) defaults to MANUAL,
        # same convention webull_bot uses for its own exit-tagged Signals.
        exit_reason_value = signal.metadata.get("exit_reason")
        exit_reason = ExitReason(exit_reason_value) if exit_reason_value else ExitReason.MANUAL
        order = Order(
            symbol=signal.symbol, side=closing_side, order_type=OrderType.MARKET, quantity=position.quantity,
            strategy_name=signal.strategy_name, exit_reason=exit_reason,
            created_at=signal.generated_at, updated_at=signal.generated_at,
        )
        filled_order = self.broker.place_order(order)
        self._record_realized_pnl(position, filled_order)
        return filled_order

    def _record_realized_pnl(self, position: Position, order: Order) -> None:
        """Feeds RiskEngine.record_trade_closed so the daily-loss limit and
        post-loss cooldown (see risk/risk_engine.py) actually know about
        this close. Finds the matching Fill via the ABC's own poll_fills
        (not a broker-specific attribute) by broker_order_id -- there's no
        `since` filter here on purpose: an Order's own created_at is
        simulated time (see _submit_exit above), but plumbing that through
        as poll_fills' `since` would be redundant (broker_order_id is
        already a unique, exact match) and add a way to silently miss a
        fill if the two clocks were ever slightly out of step."""
        if order.broker_order_id is None:
            return
        fill = next((f for f in self.broker.poll_fills() if f.order_client_id == order.broker_order_id), None)
        if fill is None:
            return
        direction = 1.0 if position.side == OrderSide.BUY else -1.0
        pnl = (fill.price - position.avg_entry_price) * position.quantity * direction
        self.risk_engine.record_trade_closed(position.symbol, pnl, now=fill.filled_at)
