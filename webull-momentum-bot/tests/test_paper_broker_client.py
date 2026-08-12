"""
Tests for PaperBrokerClient._apply_fill's cash/position bookkeeping --
previously only ever exercised indirectly (via other test files using
PaperBrokerClient as a stand-in broker) for the BUY/SELL sides. Real gap
fixed 2026-08-12: SELL_SHORT/BUY_TO_COVER had no branch at all -- a short
fill got marked FILLED and appended to the fills list, but silently
updated neither cash nor positions, with no exception raised. Currently
dormant (no strategy emits SignalAction.ENTER_SHORT yet -- see
order_manager.py's _side_for_action), but a real trap for whenever one
does.
"""
from datetime import datetime

from webull_bot.brokers.paper.client import PaperBrokerClient, PaperBrokerConfig
from webull_bot.enums import OrderSide, OrderStatus, OrderType
from webull_bot.models import MarketSnapshot, Order


def _snapshot(**overrides) -> MarketSnapshot:
    base = dict(
        symbol="TEST", timestamp=datetime(2026, 8, 11, 15, 0, 0), last_price=10.0,
        bid=9.99, ask=10.01, bid_size=100, ask_size=100, cumulative_volume=500_000,
        vwap=10.0, high_of_day=10.1, low_of_day=9.9, open_price=10.0,
    )
    base.update(overrides)
    return MarketSnapshot(**base)


def _broker(**config_overrides) -> PaperBrokerClient:
    broker = PaperBrokerClient(PaperBrokerConfig(fill_slippage_bps=0.0, **config_overrides))
    broker.connect()
    return broker


def test_buy_then_sell_updates_cash_and_closes_the_position():
    broker = _broker()
    broker.feed_snapshot(_snapshot(bid=10.0, ask=10.0, last_price=10.0))
    starting_cash = broker.get_buying_power()

    buy = broker.place_order(Order(symbol="TEST", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=100))
    assert buy.status == OrderStatus.FILLED
    assert broker.get_buying_power() == starting_cash - 1_000.0
    position = broker.get_positions()[0]
    assert position.side == OrderSide.BUY
    assert position.quantity == 100
    assert position.avg_entry_price == 10.0

    broker.feed_snapshot(_snapshot(bid=11.0, ask=11.0, last_price=11.0))
    sell = broker.place_order(Order(symbol="TEST", side=OrderSide.SELL, order_type=OrderType.MARKET, quantity=100))
    assert sell.status == OrderStatus.FILLED
    assert broker.get_positions() == []
    assert broker.get_buying_power() == starting_cash - 1_000.0 + 1_100.0


def test_sell_short_then_buy_to_cover_updates_cash_and_realizes_profit_on_a_price_drop():
    # Real bug fixed 2026-08-12: this whole scenario used to silently do
    # nothing to cash/positions (order still reported FILLED) since
    # _apply_fill had no SELL_SHORT/BUY_TO_COVER branch at all.
    broker = _broker()
    broker.feed_snapshot(_snapshot(bid=10.0, ask=10.0, last_price=10.0))
    starting_cash = broker.get_buying_power()

    short = broker.place_order(Order(symbol="TEST", side=OrderSide.SELL_SHORT, order_type=OrderType.MARKET, quantity=100))
    assert short.status == OrderStatus.FILLED
    # Proceeds received immediately, same as a real short sale.
    assert broker.get_buying_power() == starting_cash + 1_000.0
    position = broker.get_positions()[0]
    assert position.side == OrderSide.SELL_SHORT
    assert position.quantity == 100
    assert position.avg_entry_price == 10.0

    broker.feed_snapshot(_snapshot(bid=8.0, ask=8.0, last_price=8.0))  # price fell -- a short profits
    cover = broker.place_order(Order(symbol="TEST", side=OrderSide.BUY_TO_COVER, order_type=OrderType.MARKET, quantity=100))
    assert cover.status == OrderStatus.FILLED
    assert broker.get_positions() == []
    # Received $1,000 shorting, paid $800 covering -- $200 realized profit.
    assert broker.get_buying_power() == starting_cash + 1_000.0 - 800.0


def test_sell_short_partial_cover_leaves_the_remainder_open():
    broker = _broker()
    broker.feed_snapshot(_snapshot(bid=10.0, ask=10.0, last_price=10.0))
    broker.place_order(Order(symbol="TEST", side=OrderSide.SELL_SHORT, order_type=OrderType.MARKET, quantity=100))

    broker.feed_snapshot(_snapshot(bid=9.0, ask=9.0, last_price=9.0))
    broker.place_order(Order(symbol="TEST", side=OrderSide.BUY_TO_COVER, order_type=OrderType.MARKET, quantity=40))

    position = broker.get_positions()[0]
    assert position.side == OrderSide.SELL_SHORT
    assert position.quantity == 60
    assert position.realized_pnl == (10.0 - 9.0) * 40  # $40 realized on the covered slice
