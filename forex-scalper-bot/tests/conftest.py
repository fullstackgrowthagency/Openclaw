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


TEST_ACCOUNT_ID = "test-account"
TEST_TOKEN = "test-token"  # noqa: S105 -- a fixed test double credential, not a real secret


def _test_authenticator(candidate: str) -> str | None:
    return TEST_ACCOUNT_ID if candidate == TEST_TOKEN else None


@pytest.fixture
def relay_pair():
    """Yields (relay_connection, peer) -- a real RelayServer accepted
    connection paired with a real FakeRelayPeer dialed into it, already
    authenticated (every connection must auth before accept() returns it
    as of Phase 5c -- see test_relay_auth.py for the failure paths this
    fixture deliberately skips past). Both are torn down after the test
    regardless of outcome."""
    server = RelayServer(authenticator=_test_authenticator, auth_grace_seconds=5.0)
    server.start()
    peer = FakeRelayPeer()
    peer.start(f"ws://{server.host}:{server.port}")
    peer.send_auth(TEST_TOKEN, TEST_ACCOUNT_ID)
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
