"""
OrderManager: the ONLY component in this codebase allowed to call a
BrokerClient's order-placement methods.

Enforced data flow: Strategy -> RiskEngine -> OrderManager -> BrokerClient.
Strategies never receive a broker reference. If you find yourself wanting to
call `broker.place_order` from anywhere other than here, that's a sign the
architecture is being bypassed -- don't do it.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from ..config import Settings
from ..enums import OrderStatus, OrderType, SignalAction
from ..interfaces.broker import BrokerClient
from ..models import MarketSnapshot, Order, Position, RiskDecision, Signal
from ..risk.risk_engine import RiskEngine


class OrderRejected(Exception):
    def __init__(self, decision: RiskDecision):
        self.decision = decision
        super().__init__(decision.reason)


class OrderManager:
    def __init__(self, broker: BrokerClient, risk_engine: RiskEngine, settings: Settings):
        self.broker = broker
        self.risk_engine = risk_engine
        self.settings = settings

    def _side_for_action(self, action: SignalAction):
        from ..enums import OrderSide

        return {
            SignalAction.ENTER_LONG: OrderSide.BUY,
            SignalAction.SCALE_IN: OrderSide.BUY,
            SignalAction.EXIT: OrderSide.SELL,
            SignalAction.SCALE_OUT: OrderSide.SELL,
            SignalAction.ENTER_SHORT: OrderSide.SELL_SHORT,
        }[action]

    def submit_signal(self, signal: Signal, *, snapshot: MarketSnapshot, position: Optional[Position] = None) -> Order:
        """Runs a Signal through the risk engine and, if approved, places the order.
        Raises OrderRejected if the risk engine declines it -- callers must not
        retry by constructing their own Order and calling the broker directly.

        EXIT/SCALE_OUT signals deliberately skip the entry-sizing risk checks
        (spread/liquidity/exposure gates exist to control *new* risk, not to
        trap the bot in a losing position) and go straight to the broker to
        close out `position`."""

        if self.broker.is_live:
            # Belt-and-suspenders: OrderManager itself refuses to route to a live
            # broker unless the settings object says trading is fully authorized,
            # even though the broker factory and the broker's own constructor
            # already checked this.
            self.settings.require_non_live_or_authorized()

        if signal.action in (SignalAction.EXIT, SignalAction.SCALE_OUT):
            if position is None:
                raise ValueError(f"{signal.action.value} signal for {signal.symbol} requires the open position")
            quantity = position.quantity if signal.action == SignalAction.EXIT else position.quantity / 2
            order = Order(
                symbol=signal.symbol,
                side=self._side_for_action(signal.action),
                order_type=OrderType.MARKET,
                quantity=quantity,
                status=OrderStatus.PENDING,
                client_order_id=str(uuid.uuid4()),
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                strategy_name=signal.strategy_name,
            )
            return self.broker.place_order(order)

        decision = self.risk_engine.evaluate(
            signal,
            account_equity=self.broker.get_account_equity(),
            account_buying_power=self.broker.get_buying_power(),
            open_positions=self.broker.get_positions(),
            snapshot=snapshot,
        )
        if not decision.approved or not decision.max_shares:
            raise OrderRejected(decision)

        order = Order(
            symbol=signal.symbol,
            side=self._side_for_action(signal.action),
            order_type=OrderType.MARKET,
            quantity=decision.max_shares,
            status=OrderStatus.PENDING,
            client_order_id=str(uuid.uuid4()),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
            strategy_name=signal.strategy_name,
        )
        return self.broker.place_order(order)

    def cancel(self, broker_order_id: str) -> None:
        self.broker.cancel_order(broker_order_id)

    def get_status(self, broker_order_id: str) -> Order:
        return self.broker.get_order_status(broker_order_id)
