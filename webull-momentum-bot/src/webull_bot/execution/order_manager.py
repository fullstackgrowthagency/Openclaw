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

    def submit_signal(
        self,
        signal: Signal,
        *,
        snapshot: MarketSnapshot,
        position: Optional[Position] = None,
        now: Optional[datetime] = None,
    ) -> Order:
        """Runs a Signal through the risk engine and, if approved, places the order.
        Raises OrderRejected if the risk engine declines it -- callers must not
        retry by constructing their own Order and calling the broker directly.

        EXIT/SCALE_OUT signals deliberately skip the entry-sizing risk checks
        (spread/liquidity/exposure gates exist to control *new* risk, not to
        trap the bot in a losing position) and go straight to the broker to
        close out `position`.

        `now`: forwarded to RiskEngine.evaluate (entries only -- exits don't
        call evaluate at all, see above), which needs it for the core-trading-
        hours gate as well as its existing daily-rollover/cooldown checks.
        Left as None defaults evaluate() to the real wall clock
        (datetime.utcnow()), which is what every live call site wants and
        historically got implicitly. Backtests and the live trading loop
        both already compute a `now` per tick/per bar for their own state
        transitions -- pass that same value here too, so e.g. a backtest
        replaying historical bars gates entries against the *simulated*
        bar's timestamp instead of the real wall-clock time the backtest
        happens to be run at."""

        if self.broker.is_live:
            # Belt-and-suspenders: OrderManager itself refuses to route to a live
            # broker unless the settings object says trading is fully authorized,
            # even though the broker factory and the broker's own constructor
            # already checked this.
            self.settings.require_non_live_or_authorized()

        if signal.action in (SignalAction.EXIT, SignalAction.SCALE_OUT):
            if position is None:
                raise ValueError(f"{signal.action.value} signal for {signal.symbol} requires the open position")
            # SCALE_OUT sells half, floored to a whole share count --
            # PositionManager.check_exit only emits SCALE_OUT when
            # position.quantity >= 2 specifically so this is never zero.
            quantity = position.quantity if signal.action == SignalAction.EXIT else int(position.quantity // 2)
            order = Order(
                symbol=signal.symbol,
                side=self._side_for_action(signal.action),
                order_type=OrderType.MARKET,
                quantity=quantity,
                status=OrderStatus.PENDING,
                client_order_id=str(uuid.uuid4()),
                # signal.generated_at, not datetime.utcnow() -- callers use
                # this as `since` in poll_fills(since=order.created_at) to
                # find this order's fill. In backtests/paper-fed-snapshot
                # tests, fills are recorded at the *simulated* snapshot
                # timestamp, which can be arbitrarily far from the real
                # wall clock; using utcnow() here made that lookup silently
                # fail every time in that context (confirmed: a partial
                # exit's realized P&L came back as exactly $0 because the
                # fallback exit price landed on avg_entry_price instead of
                # the real fill price).
                created_at=signal.generated_at,
                updated_at=signal.generated_at,
                strategy_name=signal.strategy_name,
            )
            return self.broker.place_order(order)

        decision = self.risk_engine.evaluate(
            signal,
            account_equity=self.broker.get_account_equity(),
            account_buying_power=self.broker.get_buying_power(),
            open_positions=self.broker.get_positions(),
            snapshot=snapshot,
            now=now,
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
            created_at=signal.generated_at,  # see the exit branch above for why not datetime.utcnow()
            updated_at=signal.generated_at,
            strategy_name=signal.strategy_name,
        )
        return self.broker.place_order(order)

    def cancel(self, broker_order_id: str) -> None:
        self.broker.cancel_order(broker_order_id)

    def get_status(self, broker_order_id: str) -> Order:
        return self.broker.get_order_status(broker_order_id)

    # -- broker-side (resting) protective orders --------------------------
    # These four back TradingLoop's broker-side stop/target bracket
    # feature (see position_manager.py's module docstring and
    # WebullBrokerClient.place_oco_bracket). They deliberately don't go
    # through submit_signal: there is no Signal here (this manages an
    # already-approved, already-filled position, not a new entry/exit
    # decision that needs risk-engine sizing/exposure evaluation), and
    # submit_signal only knows how to build MARKET orders anyway. Kept on
    # OrderManager rather than called directly from TradingLoop because
    # this class is still "the ONLY component allowed to call a
    # BrokerClient's order-placement methods" (see this module's
    # docstring) -- that rule doesn't stop applying just because the order
    # being placed isn't Signal-driven.

    def _broker_supports_resting_orders(self) -> bool:
        # place_oco_bracket is not part of the BrokerClient interface (see
        # interfaces/broker.py) -- only WebullBrokerClient implements it.
        # PaperBrokerClient/backtests fill every order synchronously at
        # market regardless of order_type, so a "resting" stop placed
        # through their plain place_order would close the position
        # immediately instead of waiting for price to cross it; gating all
        # three placement methods below on this same capability check keeps that
        # broker on its existing pure-software PositionManager path
        # unchanged, exactly as it behaved before this feature existed.
        return getattr(self.broker, "place_oco_bracket", None) is not None

    def place_resting_stop(
        self, symbol: str, exit_side, quantity: float, stop_price: float,
        *, strategy_name: Optional[str] = None, now: Optional[datetime] = None,
    ) -> Optional[Order]:
        """Places a lone resting protective STOP order. Returns None
        (without calling the broker at all) if the connected broker
        doesn't support resting orders -- see _broker_supports_resting_orders."""
        if not self._broker_supports_resting_orders():
            return None
        ts = now or datetime.utcnow()
        order = Order(
            symbol=symbol, side=exit_side, order_type=OrderType.STOP, quantity=quantity,
            stop_price=stop_price, status=OrderStatus.PENDING, client_order_id=str(uuid.uuid4()),
            created_at=ts, updated_at=ts, strategy_name=strategy_name,
        )
        return self.broker.place_order(order)

    def place_resting_trailing_stop(
        self, symbol: str, exit_side, quantity: float, trailing_pct: float,
        *, strategy_name: Optional[str] = None, now: Optional[datetime] = None,
    ) -> Optional[Order]:
        """Places a lone resting protective TRAILING_STOP order -- Webull
        trails it itself from here (see WebullBrokerClient._order_payload's
        trailing_type/trailing_stop_step mapping), so unlike
        place_resting_stop's plain STOP this never needs a cancel+replace
        to move it (see TradingLoop._sync_broker_protective_orders' skip
        for Position.broker_stop_is_trailing). Only ever called by
        _attach_broker_bracket post-partial-exit, when PositionManager's
        own trailing-stop math would otherwise be the thing moving the
        resting order every tick. Returns None (without calling the broker
        at all) if the connected broker doesn't support resting orders --
        see _broker_supports_resting_orders."""
        if not self._broker_supports_resting_orders():
            return None
        ts = now or datetime.utcnow()
        order = Order(
            symbol=symbol, side=exit_side, order_type=OrderType.TRAILING_STOP, quantity=quantity,
            trailing_pct=trailing_pct, status=OrderStatus.PENDING, client_order_id=str(uuid.uuid4()),
            created_at=ts, updated_at=ts, strategy_name=strategy_name,
        )
        return self.broker.place_order(order)

    def place_resting_bracket(
        self, symbol: str, exit_side, stop_quantity: float, stop_price: float,
        target_quantity: float, target_price: float,
        *, strategy_name: Optional[str] = None, now: Optional[datetime] = None,
    ) -> Optional[tuple[Order, Order]]:
        """Places a resting OCO stop+target bracket via the broker's
        place_oco_bracket (see that method's docstring -- confirmed live
        that Webull accepts attaching this to an already-open position).
        Returns None (without calling the broker at all) under the same
        condition and for the same reason as place_resting_stop."""
        if not self._broker_supports_resting_orders():
            return None
        ts = now or datetime.utcnow()
        stop_order = Order(
            symbol=symbol, side=exit_side, order_type=OrderType.STOP, quantity=stop_quantity,
            stop_price=stop_price, status=OrderStatus.PENDING, created_at=ts, updated_at=ts,
            strategy_name=strategy_name,
        )
        target_order = Order(
            symbol=symbol, side=exit_side, order_type=OrderType.LIMIT, quantity=target_quantity,
            limit_price=target_price, status=OrderStatus.PENDING, created_at=ts, updated_at=ts,
            strategy_name=strategy_name,
        )
        return self.broker.place_oco_bracket(stop_order, target_order)

    def cancel_resting_order(self, broker_order_id: str) -> None:
        self.broker.cancel_order(broker_order_id)
