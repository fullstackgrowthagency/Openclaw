"""
Auth-gating tests that deliberately bypass the `relay_pair` fixture
(which already authenticates for every other test in this suite) to
exercise the handshake's failure paths directly: RelayServer/FakeRelayPeer
are constructed by hand here.
"""
import time

import pytest

from fx_bot.brokers.local_connector.relay_connection import AUTH_FAILURE_CLOSE_CODE
from fx_bot.brokers.local_connector.relay_server import RelayServer
from relay_protocol.envelope import Envelope
from tests.fakes.fake_relay_peer import FakeRelayPeer

ACCOUNT_ID = "acct-1"
VALID_TOKEN = "valid-token"


def _authenticator(candidate: str):
    return ACCOUNT_ID if candidate == VALID_TOKEN else None


@pytest.fixture
def server():
    srv = RelayServer(authenticator=_authenticator, auth_grace_seconds=1.0)
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


def test_authenticated_connection_exposes_account_id_and_is_queued_for_accept(server):
    peer = FakeRelayPeer()
    try:
        peer.start(f"ws://{server.host}:{server.port}")
        peer.send_auth(VALID_TOKEN, ACCOUNT_ID)

        connection = server.accept(timeout=5.0)

        assert connection.account_id == ACCOUNT_ID
        assert connection.is_connected is True
    finally:
        peer.stop()


def test_invalid_token_closes_connection_with_4401(server):
    peer = FakeRelayPeer()
    try:
        peer.start(f"ws://{server.host}:{server.port}")
        with pytest.raises(AssertionError):
            peer.send_auth("not-a-real-token", ACCOUNT_ID)

        assert peer.wait_for_close(timeout=5.0) == AUTH_FAILURE_CLOSE_CODE
        with pytest.raises(Exception):
            server.accept(timeout=0.5)
    finally:
        peer.stop()


def test_non_auth_first_frame_closes_connection_with_4401(server):
    peer = FakeRelayPeer()
    try:
        peer.start(f"ws://{server.host}:{server.port}")
        peer.send_raw(Envelope.make_event("quote", {"symbol": "EUR/USD"}).to_wire())

        assert peer.wait_for_close(timeout=5.0) == AUTH_FAILURE_CLOSE_CODE
        with pytest.raises(Exception):
            server.accept(timeout=0.5)
    finally:
        peer.stop()


def test_malformed_frame_as_first_message_closes_connection_with_4401(server):
    peer = FakeRelayPeer()
    try:
        peer.start(f"ws://{server.host}:{server.port}")
        peer.send_raw("this is not json at all")

        assert peer.wait_for_close(timeout=5.0) == AUTH_FAILURE_CLOSE_CODE
    finally:
        peer.stop()


def test_no_auth_frame_within_grace_period_times_out_and_closes():
    srv = RelayServer(authenticator=_authenticator, auth_grace_seconds=0.3)
    srv.start()
    peer = FakeRelayPeer()
    try:
        peer.start(f"ws://{srv.host}:{srv.port}")
        # Send nothing at all -- the grace-period timeout, not the
        # first-frame-must-be-auth rule, is what must catch this.
        assert peer.wait_for_close(timeout=5.0) == AUTH_FAILURE_CLOSE_CODE
    finally:
        peer.stop()
        srv.stop()


def test_late_duplicate_auth_frame_after_handshake_is_ignored_not_processed_as_request(server):
    peer = FakeRelayPeer()
    try:
        peer.start(f"ws://{server.host}:{server.port}")
        peer.send_auth(VALID_TOKEN, ACCOUNT_ID)
        connection = server.accept(timeout=5.0)

        # A second, late auth frame on an already-authenticated connection.
        peer.send_raw(Envelope.make_auth(token=VALID_TOKEN, account_id=ACCOUNT_ID).to_wire())
        time.sleep(0.1)  # give _handle_frame a moment to process (and ignore) it

        # The connection must still be fully usable afterward -- proving
        # the duplicate frame didn't corrupt state or crash the read loop.
        peer.script_response("get_account_equity", {"equity": 5_000.0})
        result = connection.send_request("get_account_equity", {})
        assert result == {"equity": 5_000.0}
        assert connection.account_id == ACCOUNT_ID
    finally:
        peer.stop()
