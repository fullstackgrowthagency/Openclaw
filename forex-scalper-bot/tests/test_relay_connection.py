import time

import pytest

from fx_bot.brokers.local_connector.exceptions import (
    BrokerRejectedError,
    ConnectorOfflineError,
    ConnectorTimeoutError,
)


def test_request_response_round_trip(relay_pair):
    connection, peer = relay_pair
    peer.script_response("get_account_equity", {"equity": 12_345.0})

    result = connection.send_request("get_account_equity", {})

    assert result == {"equity": 12_345.0}


def test_event_dispatches_to_registered_handler(relay_pair):
    connection, peer = relay_pair
    received = []
    connection.register_event_handler("quote", received.append)

    peer.push_event("quote", {"symbol": "EUR/USD", "bid": 1.1000, "ask": 1.1002})
    _wait_until(lambda: len(received) == 1)

    assert received[0] == {"symbol": "EUR/USD", "bid": 1.1000, "ask": 1.1002}


def test_unregistered_handler_is_not_called(relay_pair):
    connection, peer = relay_pair
    received = []
    connection.register_event_handler("quote", received.append)
    connection.unregister_event_handler("quote", received.append)

    peer.push_event("quote", {"symbol": "EUR/USD", "bid": 1.1000, "ask": 1.1002})
    time.sleep(0.2)

    assert received == []


def test_send_request_raises_connector_offline_error_when_not_connected(relay_pair):
    connection, peer = relay_pair
    connection.close()
    time.sleep(0.2)

    with pytest.raises(ConnectorOfflineError):
        connection.send_request("get_account_equity", {})


def test_send_request_raises_connector_offline_error_on_drop_mid_request(relay_pair):
    connection, peer = relay_pair
    peer.script_drop("get_free_margin")

    with pytest.raises(ConnectorOfflineError):
        connection.send_request("get_free_margin", {})


def test_send_request_raises_connector_timeout_error_past_deadline(relay_pair):
    connection, peer = relay_pair
    peer.script_delay("get_positions", {"positions": []}, delay_seconds=1.0)

    with pytest.raises(ConnectorTimeoutError):
        connection.send_request("get_positions", {}, timeout=0.2)


def test_send_request_raises_broker_rejected_error_on_error_envelope(relay_pair):
    connection, peer = relay_pair
    peer.script_error("place_order", error_type="BrokerRejectedError", message="Insufficient margin.")

    with pytest.raises(BrokerRejectedError) as exc_info:
        connection.send_request("place_order", {})

    assert exc_info.value.error_type == "BrokerRejectedError"
    assert exc_info.value.message == "Insufficient margin."


def test_is_connected_reflects_socket_state(relay_pair):
    connection, peer = relay_pair
    assert connection.is_connected is True

    peer.stop()
    _wait_until(lambda: connection.is_connected is False)


def _wait_until(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError("Condition not met within timeout.")
