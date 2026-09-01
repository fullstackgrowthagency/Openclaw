"""
Conversion between relay_protocol's Wire* Pydantic models and fx_bot's
own domain dataclasses. Lives here, not in relay_protocol itself -- see
that package's wire_models.py docstring: relay_protocol must stay
importable from the (future) Windows-only connector process without
needing fx_bot's enum vocabulary kept in sync as it grows, so validating
a wire string against a real fx_bot enum is this module's job
specifically. A malformed/unrecognized wire value (e.g. OrderSide("bogus"))
deliberately raises a bare ValueError rather than a wrapped exception --
see local_connector/exceptions.py's docstring for why.
"""
from __future__ import annotations

from relay_protocol.wire_models import WireFill, WireMarketSnapshot, WireOrder, WirePosition

from ...enums import ExitReason, OrderSide, OrderStatus, OrderType, TimeInForce
from ...models import Fill, MarketSnapshot, Order, Position


def wire_snapshot_to_snapshot(wire: WireMarketSnapshot) -> MarketSnapshot:
    return MarketSnapshot(symbol=wire.symbol, timestamp=wire.timestamp, bid=wire.bid, ask=wire.ask)


def snapshot_to_wire_snapshot(snapshot: MarketSnapshot) -> WireMarketSnapshot:
    return WireMarketSnapshot(symbol=snapshot.symbol, timestamp=snapshot.timestamp, bid=snapshot.bid, ask=snapshot.ask)


def wire_order_to_order(wire: WireOrder) -> Order:
    return Order(
        symbol=wire.symbol, side=OrderSide(wire.side), order_type=OrderType(wire.order_type),
        quantity=wire.quantity, time_in_force=TimeInForce(wire.time_in_force),
        limit_price=wire.limit_price, stop_price=wire.stop_price,
        stop_loss_price=wire.stop_loss_price, take_profit_price=wire.take_profit_price,
        trailing_pips=wire.trailing_pips,
        exit_reason=ExitReason(wire.exit_reason) if wire.exit_reason else None,
        status=OrderStatus(wire.status),
        client_order_id=wire.client_order_id, broker_order_id=wire.broker_order_id,
        created_at=wire.created_at, updated_at=wire.updated_at,
        strategy_name=wire.strategy_name, signal_id=wire.signal_id,
    )


def order_to_wire_order(order: Order) -> WireOrder:
    return WireOrder(
        symbol=order.symbol, side=order.side.value, order_type=order.order_type.value,
        quantity=order.quantity, time_in_force=order.time_in_force.value,
        limit_price=order.limit_price, stop_price=order.stop_price,
        stop_loss_price=order.stop_loss_price, take_profit_price=order.take_profit_price,
        trailing_pips=order.trailing_pips,
        exit_reason=order.exit_reason.value if order.exit_reason else None,
        status=order.status.value,
        client_order_id=order.client_order_id, broker_order_id=order.broker_order_id,
        created_at=order.created_at, updated_at=order.updated_at,
        strategy_name=order.strategy_name, signal_id=order.signal_id,
    )


def wire_fill_to_fill(wire: WireFill) -> Fill:
    return Fill(
        order_client_id=wire.order_client_id, symbol=wire.symbol, side=OrderSide(wire.side),
        quantity=wire.quantity, price=wire.price, filled_at=wire.filled_at, fees=wire.fees,
    )


def fill_to_wire_fill(fill: Fill) -> WireFill:
    return WireFill(
        order_client_id=fill.order_client_id, symbol=fill.symbol, side=fill.side.value,
        quantity=fill.quantity, price=fill.price, filled_at=fill.filled_at, fees=fill.fees,
    )


def wire_position_to_position(wire: WirePosition) -> Position:
    return Position(
        symbol=wire.symbol, side=OrderSide(wire.side), quantity=wire.quantity,
        avg_entry_price=wire.avg_entry_price, stop_price=wire.stop_price, target_price=wire.target_price,
        trailing_stop_pips=wire.trailing_stop_pips, opened_at=wire.opened_at, strategy_name=wire.strategy_name,
        entry_signal_id=wire.entry_signal_id, realized_pnl=wire.realized_pnl,
        max_favorable_excursion=wire.max_favorable_excursion, max_adverse_excursion=wire.max_adverse_excursion,
        partial_exit_taken=wire.partial_exit_taken, swap=wire.swap,
    )


def position_to_wire_position(position: Position) -> WirePosition:
    return WirePosition(
        symbol=position.symbol, side=position.side.value, quantity=position.quantity,
        avg_entry_price=position.avg_entry_price, stop_price=position.stop_price, target_price=position.target_price,
        trailing_stop_pips=position.trailing_stop_pips, opened_at=position.opened_at,
        strategy_name=position.strategy_name, entry_signal_id=position.entry_signal_id,
        realized_pnl=position.realized_pnl, max_favorable_excursion=position.max_favorable_excursion,
        max_adverse_excursion=position.max_adverse_excursion, partial_exit_taken=position.partial_exit_taken,
        swap=position.swap,
    )
