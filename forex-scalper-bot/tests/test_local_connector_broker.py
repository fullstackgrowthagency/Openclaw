from datetime import datetime, timezone

import pytest

from fx_bot.brokers.local_connector.exceptions import BrokerRejectedError, ConnectorOfflineError, ConnectorTimeoutError
from fx_bot.enums import OrderSide, OrderType
from fx_bot.models import MarketSnapshot, Order


def _wire_order_payload(**overrides) -> dict:
    payload = {
        "symbol": "EUR/USD", "side": "buy", "order_type": "market", "quantity": 10_000,
        "time_in_force": "day", "limit_price": None, "stop_price": None,
        "stop_loss_price": 1.0950, "take_profit_price": 1.1050, "trailing_pips": None,
        "exit_reason": None, "status": "filled", "client_order_id": None, "broker_order_id": "mt5-1",
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
        "strategy_name": "scalper_v1", "signal_id": None,
    }
    payload.update(overrides)
    return payload


def test_place_order_returns_filled_order_with_broker_order_id(local_connector_broker):
    broker, peer = local_connector_broker
    peer.script_response("place_order", _wire_order_payload())

    order = Order(symbol="EUR/USD", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=10_000)
    filled = broker.place_order(order)

    assert filled.broker_order_id == "mt5-1"
    assert filled.side == OrderSide.BUY


def test_place_order_raises_broker_rejected_error_on_mt5_rejection(local_connector_broker):
    broker, peer = local_connector_broker
    peer.script_error("place_order", error_type="TRADE_RETCODE_NO_MONEY", message="Not enough money.")

    order = Order(symbol="EUR/USD", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=10_000)
    with pytest.raises(BrokerRejectedError):
        broker.place_order(order)


def test_poll_fills_raises_connector_timeout_error_when_connector_is_slow(relay_pair):
    from fx_bot.brokers.local_connector.broker import LocalConnectorBroker

    connection, peer = relay_pair
    broker = LocalConnectorBroker(connection, request_timeout=0.2)
    peer.script_delay("poll_fills", {"fills": []}, delay_seconds=1.0)

    with pytest.raises(ConnectorTimeoutError):
        broker.poll_fills()


def test_subscribe_quotes_invokes_on_update_for_matching_symbol_only(local_connector_broker):
    broker, peer = local_connector_broker
    peer.script_response("subscribe_quotes", {})
    received = []

    broker.subscribe_quotes(["EUR/USD"], received.append)
    peer.push_event("quote", {"symbol": "EUR/USD", "timestamp": "2026-01-01T00:00:00Z", "bid": 1.1000, "ask": 1.1002})
    peer.push_event("quote", {"symbol": "GBP/USD", "timestamp": "2026-01-01T00:00:00Z", "bid": 1.2500, "ask": 1.2502})

    _wait_for(lambda: len(received) == 1)

    assert isinstance(received[0], MarketSnapshot)
    assert received[0].symbol == "EUR/USD"


def test_unsubscribe_quotes_stops_further_callbacks(local_connector_broker):
    broker, peer = local_connector_broker
    peer.script_response("subscribe_quotes", {})
    peer.script_response("unsubscribe_quotes", {})
    received = []

    broker.subscribe_quotes(["EUR/USD"], received.append)
    broker.unsubscribe_quotes(["EUR/USD"])
    peer.push_event("quote", {"symbol": "EUR/USD", "timestamp": "2026-01-01T00:00:00Z", "bid": 1.1000, "ask": 1.1002})

    import time
    time.sleep(0.3)
    assert received == []


def test_get_last_known_positions_raises_before_any_successful_read(local_connector_broker):
    broker, _peer = local_connector_broker
    with pytest.raises(RuntimeError):
        broker.get_last_known_positions()


def test_get_last_known_positions_reflects_the_last_successful_get_positions_call(local_connector_broker):
    broker, peer = local_connector_broker
    peer.script_response("get_positions", {"positions": []})

    broker.get_positions()
    positions, as_of = broker.get_last_known_positions()

    assert positions == []
    assert isinstance(as_of, datetime)


def test_get_positions_raises_connector_offline_error_rather_than_falling_back_to_cache(local_connector_broker):
    broker, peer = local_connector_broker
    peer.script_response("get_positions", {"positions": []})
    broker.get_positions()  # prime the cache

    peer.script_drop("get_positions")
    with pytest.raises(ConnectorOfflineError):
        broker.get_positions()

    # The cache from the earlier successful call must still be readable --
    # get_last_known_positions is display-only, unaffected by a later
    # failed live read.
    positions, _as_of = broker.get_last_known_positions()
    assert positions == []


def test_is_live_reflects_injected_flag_not_hardcoded(relay_pair):
    from fx_bot.brokers.local_connector.broker import LocalConnectorBroker

    connection, _peer = relay_pair
    demo_broker = LocalConnectorBroker(connection, is_live=False)
    assert demo_broker.is_live is False

    live_broker = LocalConnectorBroker(connection, is_live=True)
    assert live_broker.is_live is True


def _wait_for(condition, timeout: float = 5.0) -> None:
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError("Condition not met within timeout.")
