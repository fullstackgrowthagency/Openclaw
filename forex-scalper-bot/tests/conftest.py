"""
Shared fixtures for the local-connector relay test suite -- see
tests/fakes/fake_relay_peer.py's docstring for why this drives real
sockets rather than an in-memory mock.
"""
from __future__ import annotations

import pytest

from fx_bot.brokers.local_connector.broker import LocalConnectorBroker
from fx_bot.brokers.local_connector.relay_server import RelayServer
from tests.fakes.fake_relay_peer import FakeRelayPeer


@pytest.fixture
def relay_pair():
    """Yields (relay_connection, peer) -- a real RelayServer accepted
    connection paired with a real FakeRelayPeer dialed into it. Both are
    torn down after the test regardless of outcome."""
    server = RelayServer()
    server.start()
    peer = FakeRelayPeer()
    peer.start(f"ws://{server.host}:{server.port}")
    connection = server.accept(timeout=5.0)
    try:
        yield connection, peer
    finally:
        peer.stop()
        connection.close()
        server.stop()


@pytest.fixture
def local_connector_broker(relay_pair):
    connection, peer = relay_pair
    broker = LocalConnectorBroker(connection)
    return broker, peer
