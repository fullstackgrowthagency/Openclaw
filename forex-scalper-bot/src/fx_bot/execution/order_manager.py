"""
Order manager -- the only module wired to a BrokerClient (see that ABC's
own docstring). Converts an approved Signal into an Order and submits it.
Mirrors webull-momentum-bot's execution/order_manager.py in role; kept
minimal for this Phase 2 skeleton -- SCALE_IN/SCALE_OUT (partial exits)
aren't implemented yet, same deferral the equities bot itself made (that
project's own partial-exit support landed well after its initial
skeleton, once PositionManager existed to drive it).
"""
from __future__ import annotations

from typing import Optional

from ..enums import OrderSide, OrderType, SignalAction
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
        decision = self.risk_engine.evaluate(signal, open_positions=open_positions, snapshot=snapshot)
        if not decision.approved:
            return None
        side = OrderSide.BUY if signal.action == SignalAction.ENTER_LONG else OrderSide.SELL
        order = Order(
            symbol=signal.symbol, side=side, order_type=OrderType.MARKET, quantity=decision.max_units,
            stop_loss_price=signal.suggested_stop, take_profit_price=signal.suggested_target,
            strategy_name=signal.strategy_name, signal_id=signal.metadata.get("signal_id"),
        )
        return self.broker.place_order(order)

    def _submit_exit(self, signal: Signal, *, open_positions: list[Position]) -> Optional[Order]:
        position = next((p for p in open_positions if p.symbol == signal.symbol), None)
        if position is None:
            return None  # nothing open for this symbol -- a stale/duplicate exit signal, not an error
        closing_side = OrderSide.SELL if position.side == OrderSide.BUY else OrderSide.BUY
        order = Order(
            symbol=signal.symbol, side=closing_side, order_type=OrderType.MARKET, quantity=position.quantity,
            strategy_name=signal.strategy_name,
        )
        return self.broker.place_order(order)
