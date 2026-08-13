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
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from ..config import Settings
from ..enums import OrderSide, OrderStatus, OrderType, SignalAction
from ..interfaces.broker import BrokerClient
from ..market_hours import is_within_core_trading_hours
from ..models import MarketSnapshot, Order, Position, RiskDecision, Signal
from ..risk.risk_engine import RiskEngine


class OrderRejected(Exception):
    def __init__(self, decision: RiskDecision):
        self.decision = decision
        super().__init__(decision.reason)


class BracketEntryRejected(Exception):
    """Raised by submit_entry_signal when the broker supports atomic
    bracket entries (WebullBrokerClient.place_bracket_entry) but rejects
    this particular combo request -- distinct from OrderRejected (a
    RiskEngine sizing/exposure decision, made before any broker call) and
    from a generic broker/network Exception, so TradingLoop can tell "the
    broker refused to bracket this entry" apart from "something else went
    wrong" and surface a specific, actionable reason. Per explicit
    instruction (2026-08-13, see docs/ARCHITECTURE.md's "Atomic bracket
    entry" section): when this is raised, the trade must NOT go through at
    all -- no fallback to an unprotected plain entry."""
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass
class BracketSubmissionResult:
    """Return shape for submit_entry_signal -- entry_order is always set
    (the same contract submit_signal's callers already expect); stop_order/
    target_order are None when no atomic bracket was attempted (the broker
    lacks place_bracket_entry, e.g. PaperBrokerClient/backtests, or the
    signal itself has no suggested_stop/suggested_target), non-None when it
    was. TradingLoop uses that distinction to decide whether
    _confirm_entry_filled still needs its own separate _attach_broker_bracket
    call (see that method's docstring)."""
    entry_order: Order
    stop_order: Optional[Order] = None
    target_order: Optional[Order] = None


class OrderManager:
    # Marketable-limit buffer (2026-08-12) for entries/exits placed outside
    # core hours -- see submit_signal's "extended_hours" branch below. Not a
    # real economic edge given up on purpose: a plain MARKET order is
    # apparently not accepted at all outside core hours (confirmed live --
    # a resting OCO stop+target bracket built from STOP_LOSS+LIMIT legs was
    # rejected with support_trading_session="ALL" at 9:10am ET pre-market,
    # the same OAUTH_OPENAPI_PARAM_ERR as the original 2026-08-10 finding,
    # even though a plain LIMIT order tested clean minutes earlier -- see
    # brokers/webull/client.py's _order_payload docstring). A LIMIT order
    # priced this far through the current quote should fill essentially
    # like a market order in normal conditions while staying within
    # whatever the broker actually accepts pre-market/after-hours.
    EXTENDED_HOURS_LIMIT_BUFFER_PCT = 0.5

    def __init__(self, broker: BrokerClient, risk_engine: RiskEngine, settings: Settings):
        self.broker = broker
        self.risk_engine = risk_engine
        self.settings = settings

    def _side_for_action(self, action: SignalAction):
        return {
            SignalAction.ENTER_LONG: OrderSide.BUY,
            SignalAction.SCALE_IN: OrderSide.BUY,
            SignalAction.EXIT: OrderSide.SELL,
            SignalAction.SCALE_OUT: OrderSide.SELL,
            SignalAction.ENTER_SHORT: OrderSide.SELL_SHORT,
        }[action]

    def _order_type_and_limit_price(
        self, side: OrderSide, snapshot: MarketSnapshot, now: datetime
    ) -> tuple[OrderType, Optional[float]]:
        """MARKET (no limit_price) during core hours -- unchanged behavior.
        Outside core hours (pre-market/after-hours, gated by
        RiskConfig.allow_extended_hours_trading before a signal even
        reaches here for entries; always possible for an exit if a
        position is still open when core hours end), a marketable LIMIT
        instead: buy-side orders (BUY/BUY_TO_COVER) priced
        EXTENDED_HOURS_LIMIT_BUFFER_PCT above the current ask, sell-side
        orders (SELL/SELL_SHORT) priced the same % below the current bid
        -- aggressive enough to fill like a market order under normal
        conditions. Falls back to snapshot.last_price when bid/ask are
        both falsy (e.g. a very thin pre-market quote), same fallback
        RiskEngine.evaluate's own spread calculation uses."""
        if is_within_core_trading_hours(now):
            return OrderType.MARKET, None
        buy_like = side in (OrderSide.BUY, OrderSide.BUY_TO_COVER)
        reference = (snapshot.ask if buy_like else snapshot.bid) or snapshot.last_price
        buffer = reference * self.EXTENDED_HOURS_LIMIT_BUFFER_PCT / 100.0
        limit_price = reference + buffer if buy_like else reference - buffer
        return OrderType.LIMIT, round(limit_price, 2)

    def submit_signal(
        self,
        signal: Signal,
        *,
        snapshot: MarketSnapshot,
        position: Optional[Position] = None,
        open_positions: Optional[list[Position]] = None,
        now: Optional[datetime] = None,
    ) -> Order:
        """Runs a Signal through the risk engine and, if approved, places the order.
        Raises OrderRejected if the risk engine declines it -- callers must not
        retry by constructing their own Order and calling the broker directly.

        EXIT/SCALE_OUT signals deliberately skip the entry-sizing risk checks
        (spread/liquidity/exposure gates exist to control *new* risk, not to
        trap the bot in a losing position) and go straight to the broker to
        close out `position`.

        `open_positions` (entries only -- required there, see the ValueError
        below; irrelevant for EXIT/SCALE_OUT): the caller's own
        locally-tracked open positions, e.g. TradingLoop's `self._positions`
        or BacktestEngine's broker-held ones -- deliberately NOT fetched
        internally via `self.broker.get_positions()` the way it briefly was.
        Real bug fixed 2026-08-11: WebullBrokerClient._position_from_dict and
        PaperBrokerClient's own get_positions() both hard-code
        `stop_price=None` on every Position they return (there's no such
        field in a broker's raw account-positions response -- a stop is a
        separate resting order, or a purely local concept for a
        software-managed position, never a property of the position row
        itself), so RiskEngine.evaluate's `max_total_risk_pct` gate -- which
        filters on `p.stop_price is not None` to sum up assumed risk across
        open positions -- silently saw an empty set and NEVER rejected an
        entry no matter how much risk was already on, in every trading mode.
        Only the caller's own locally-tracked positions (set from each
        entry's own suggested_stop, kept current by breakeven/trailing math)
        carry a real stop_price. `max_simultaneous_positions`'s count-based
        gate was unaffected by this bug (it only needs len(), not
        stop_price), which is exactly why it went unnoticed for so long.

        `now`: forwarded to RiskEngine.evaluate (entries only -- exits don't
        call evaluate at all, see above), which needs it for the core-trading-
        hours gate as well as its existing daily-rollover/cooldown checks.
        Also used (both entries AND exits, 2026-08-12) by
        _order_type_and_limit_price to decide MARKET vs. a marketable LIMIT
        -- see that method's docstring. Left as None defaults to the real
        wall clock (datetime.utcnow(), resolved once into `effective_now`
        below), which is what every live call site wants and historically
        got implicitly. Backtests and the live trading loop both already
        compute a `now` per tick/per bar for their own state transitions --
        pass that same value here too, so e.g. a backtest replaying
        historical bars gates entries against the *simulated* bar's
        timestamp instead of the real wall-clock time the backtest happens
        to be run at."""

        if self.broker.is_live:
            # Belt-and-suspenders: OrderManager itself refuses to route to a live
            # broker unless the settings object says trading is fully authorized,
            # even though the broker factory and the broker's own constructor
            # already checked this.
            self.settings.require_non_live_or_authorized()

        effective_now = now or datetime.utcnow()

        if signal.action in (SignalAction.EXIT, SignalAction.SCALE_OUT):
            if position is None:
                raise ValueError(f"{signal.action.value} signal for {signal.symbol} requires the open position")
            # SCALE_OUT sells half, floored to a whole share count --
            # PositionManager.check_exit only emits SCALE_OUT when
            # position.quantity >= 2 specifically so this is never zero.
            quantity = position.quantity if signal.action == SignalAction.EXIT else int(position.quantity // 2)
            exit_side = self._side_for_action(signal.action)
            order_type, limit_price = self._order_type_and_limit_price(exit_side, snapshot, effective_now)
            order = Order(
                symbol=signal.symbol,
                side=exit_side,
                order_type=order_type,
                limit_price=limit_price,
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

        if open_positions is None:
            raise ValueError(
                f"{signal.action.value} signal for {signal.symbol} requires open_positions "
                "(the caller's own locally-tracked positions, not broker.get_positions() -- "
                "see this method's docstring for why)"
            )
        decision = self.risk_engine.evaluate(
            signal,
            account_equity=self.broker.get_account_equity(),
            account_buying_power=self.broker.get_buying_power(),
            open_positions=open_positions,
            snapshot=snapshot,
            now=now,
        )
        if not decision.approved or not decision.max_shares:
            raise OrderRejected(decision)

        entry_side = self._side_for_action(signal.action)
        order_type, limit_price = self._order_type_and_limit_price(entry_side, snapshot, effective_now)
        order = Order(
            symbol=signal.symbol,
            side=entry_side,
            order_type=order_type,
            limit_price=limit_price,
            quantity=decision.max_shares,
            status=OrderStatus.PENDING,
            client_order_id=str(uuid.uuid4()),
            created_at=signal.generated_at,  # see the exit branch above for why not datetime.utcnow()
            updated_at=signal.generated_at,
            strategy_name=signal.strategy_name,
        )
        return self.broker.place_order(order)

    def submit_entry_signal(
        self,
        signal: Signal,
        *,
        snapshot: MarketSnapshot,
        open_positions: list[Position],
        now: Optional[datetime] = None,
    ) -> BracketSubmissionResult:
        """Entry-only counterpart to submit_signal, added 2026-08-13 (see
        docs/ARCHITECTURE.md's "Atomic bracket entry" section) as a
        SEPARATE method rather than a branch inside submit_signal: an
        entry -- unlike an exit, and unlike submit_signal's existing plain-
        entry path -- may place its stop/target as part of the SAME broker
        call as the entry itself (see WebullBrokerClient.place_bracket_entry),
        and the caller needs those resulting Order objects back to populate
        the freshly-opened Position's broker_stop_order_id/
        broker_target_order_id directly, without TradingLoop
        ._attach_broker_bracket placing a second, duplicate bracket on top
        of one already resting at the broker. submit_signal itself is
        deliberately left untouched -- BacktestEngine and anything else
        that only wants a plain entry order keep working exactly as before.

        Runs the exact same RiskEngine.evaluate sizing/exposure gate as
        submit_signal's entry branch (raises OrderRejected on disapproval,
        identically). If the connected broker doesn't implement
        place_bracket_entry (PaperBrokerClient/backtests -- checked via
        getattr, same pattern as OrderManager's resting-order helpers) or
        the signal itself has no suggested_stop/suggested_target, falls
        back to a plain, unbracketed entry order -- same shape submit_signal
        would have placed. That is NOT the same thing as a rejection: a
        broker/signal that never had the capability to begin with hasn't
        refused anything.

        A broker that DOES support atomic bracket entries and rejects this
        specific combo request raises BracketEntryRejected instead -- per
        explicit instruction, callers (TradingLoop._submit_entry) must NOT
        fall back to a plain unprotected entry on that failure; the trade
        must not go through at all."""
        effective_now = now or datetime.utcnow()
        decision = self.risk_engine.evaluate(
            signal,
            account_equity=self.broker.get_account_equity(),
            account_buying_power=self.broker.get_buying_power(),
            open_positions=open_positions,
            snapshot=snapshot,
            now=now,
        )
        if not decision.approved or not decision.max_shares:
            raise OrderRejected(decision)

        entry_side = self._side_for_action(signal.action)
        order_type, limit_price = self._order_type_and_limit_price(entry_side, snapshot, effective_now)
        entry_order = Order(
            symbol=signal.symbol,
            side=entry_side,
            order_type=order_type,
            limit_price=limit_price,
            quantity=decision.max_shares,
            status=OrderStatus.PENDING,
            client_order_id=str(uuid.uuid4()),
            created_at=signal.generated_at,
            updated_at=signal.generated_at,
            strategy_name=signal.strategy_name,
        )

        place_bracket_entry = getattr(self.broker, "place_bracket_entry", None)
        if place_bracket_entry is None or signal.suggested_stop is None or signal.suggested_target is None:
            return BracketSubmissionResult(entry_order=self.broker.place_order(entry_order))

        exit_side = OrderSide.SELL if entry_side == OrderSide.BUY else OrderSide.BUY_TO_COVER
        stop_order = Order(
            symbol=signal.symbol,
            side=exit_side,
            order_type=OrderType.STOP,
            quantity=decision.max_shares,
            stop_price=signal.suggested_stop,
            status=OrderStatus.PENDING,
            client_order_id=str(uuid.uuid4()),
            created_at=signal.generated_at,
            updated_at=signal.generated_at,
            strategy_name=signal.strategy_name,
        )
        # Mirrors TradingLoop._attach_broker_bracket's own halving rule
        # exactly: the take-profit leg is a partial exit (sell half, keep
        # half riding the stop/trailing-stop -- see PositionManager's
        # SCALE_OUT design), so a target leg is only included at all if
        # half the entry quantity rounds to at least one whole share.
        target_quantity = int(decision.max_shares // 2)
        target_order = None
        if target_quantity >= 1:
            target_order = Order(
                symbol=signal.symbol,
                side=exit_side,
                order_type=OrderType.LIMIT,
                quantity=target_quantity,
                limit_price=signal.suggested_target,
                status=OrderStatus.PENDING,
                client_order_id=str(uuid.uuid4()),
                created_at=signal.generated_at,
                updated_at=signal.generated_at,
                strategy_name=signal.strategy_name,
            )

        try:
            entry_result, stop_result, target_result = place_bracket_entry(entry_order, stop_order, target_order)
        except Exception as exc:
            raise BracketEntryRejected(str(exc)) from exc
        return BracketSubmissionResult(entry_order=entry_result, stop_order=stop_result, target_order=target_result)

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
