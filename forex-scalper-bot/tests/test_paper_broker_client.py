from datetime import datetime

import pytest

from fx_bot.brokers.paper.client import PaperBrokerClient
from fx_bot.enums import ExitReason, OrderSide, OrderStatus, OrderType
from fx_bot.models import MarketSnapshot, Order


def _snapshot(bid=1.0999, ask=1.1001) -> MarketSnapshot:
    return MarketSnapshot(symbol="EUR/USD", timestamp=datetime.utcnow(), bid=bid, ask=ask)


def test_get_snapshot_raises_before_anything_is_fed():
    broker = PaperBrokerClient()
    with pytest.raises(KeyError):
        broker.get_snapshot("EUR/USD")


def test_feed_snapshot_makes_it_available_via_get_snapshot_and_get_bars():
    broker = PaperBrokerClient()
    snapshot = _snapshot()
    broker.feed_snapshot(snapshot)
    assert broker.get_snapshot("EUR/USD") is snapshot
    assert broker.get_bars("EUR/USD", "1m", lookback=10) == [snapshot]


def test_buy_market_order_fills_at_the_ask():
    broker = PaperBrokerClient()
    broker.feed_snapshot(_snapshot(bid=1.0999, ask=1.1001))
    order = Order(symbol="EUR/USD", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=10_000)

    filled = broker.place_order(order)

    assert filled.status == OrderStatus.FILLED
    assert filled.broker_order_id is not None
    position = broker.get_positions()[0]
    assert position.avg_entry_price == pytest.approx(1.1001)
    assert position.quantity == 10_000


def test_sell_market_order_fills_at_the_bid():
    broker = PaperBrokerClient()
    broker.feed_snapshot(_snapshot(bid=1.0999, ask=1.1001))
    order = Order(symbol="EUR/USD", side=OrderSide.SELL, order_type=OrderType.MARKET, quantity=10_000)

    broker.place_order(order)

    position = broker.get_positions()[0]
    assert position.avg_entry_price == pytest.approx(1.0999)


def test_slippage_pips_widens_the_effective_fill_price():
    broker = PaperBrokerClient(slippage_pips=2.0)
    broker.feed_snapshot(_snapshot(bid=1.0999, ask=1.1001))
    buy = broker.place_order(Order(symbol="EUR/USD", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=10_000))
    assert broker.get_positions()[0].avg_entry_price == pytest.approx(1.1001 + 0.0002)


def test_fill_and_position_timestamps_use_simulated_time_not_wall_clock():
    # A backtest replaying 2026-01-01 data must produce 2026-01-01 trade
    # records, not "whenever this process happened to run in real time" --
    # real incident caught by test_rule_builder_parity.py's parity proof,
    # where two backtest runs a few milliseconds apart in wall-clock time
    # produced non-identical timestamps despite identical simulated data.
    broker = PaperBrokerClient()
    simulated_time = datetime(2020, 1, 1, 9, 30, 0)
    broker.feed_snapshot(MarketSnapshot(symbol="EUR/USD", timestamp=simulated_time, bid=1.0999, ask=1.1001))

    filled = broker.place_order(Order(symbol="EUR/USD", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=10_000))

    assert filled.updated_at == simulated_time
    assert broker.get_positions()[0].opened_at == simulated_time


def test_opposite_side_order_closes_the_position_and_records_a_trade():
    broker = PaperBrokerClient(initial_equity=10_000.0)
    broker.feed_snapshot(_snapshot(bid=1.0999, ask=1.1001))
    broker.place_order(Order(symbol="EUR/USD", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=10_000))

    broker.feed_snapshot(_snapshot(bid=1.1049, ask=1.1051))
    broker.place_order(Order(symbol="EUR/USD", side=OrderSide.SELL, order_type=OrderType.MARKET, quantity=10_000))

    assert broker.get_positions() == []
    assert len(broker.trades) == 1
    trade = broker.trades[0]
    assert trade.exit_reason == ExitReason.MANUAL
    assert trade.entry_price == pytest.approx(1.1001)
    assert trade.exit_price == pytest.approx(1.1049)
    assert trade.pnl == pytest.approx((1.1049 - 1.1001) * 10_000)
    assert broker.get_account_equity() == pytest.approx(10_000.0 + trade.pnl)


def test_short_position_profits_when_price_falls():
    broker = PaperBrokerClient(initial_equity=10_000.0)
    broker.feed_snapshot(_snapshot(bid=1.1049, ask=1.1051))
    broker.place_order(Order(symbol="EUR/USD", side=OrderSide.SELL, order_type=OrderType.MARKET, quantity=10_000))

    broker.feed_snapshot(_snapshot(bid=1.0999, ask=1.1001))
    broker.place_order(Order(symbol="EUR/USD", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=10_000))

    trade = broker.trades[0]
    assert trade.pnl > 0  # sold high, bought back low


def test_limit_orders_are_not_yet_supported():
    broker = PaperBrokerClient()
    broker.feed_snapshot(_snapshot())
    order = Order(symbol="EUR/USD", side=OrderSide.BUY, order_type=OrderType.LIMIT, quantity=10_000, limit_price=1.09)
    with pytest.raises(NotImplementedError):
        broker.place_order(order)


def test_poll_fills_returns_everything_since_the_given_time():
    broker = PaperBrokerClient()
    broker.feed_snapshot(_snapshot())
    broker.place_order(Order(symbol="EUR/USD", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=10_000))
    assert len(broker.poll_fills()) == 1
    assert len(broker.poll_fills(since=datetime.utcnow())) == 0


def test_is_live_is_always_false():
    assert PaperBrokerClient().is_live is False
