import asyncio
from datetime import datetime, timezone

import pytest
from relay_protocol.envelope import EnvelopeKind
from relay_protocol.wire_models import WireMarketSnapshot

from fx_connector.mt5_client import MT5Client
from fx_connector.mt5_executor import MT5Executor
from fx_connector.relay_client import AuthFailure, RelayClient
from tests.fakes.fake_cloud_peer import FakeCloudPeer
from tests.fakes.fake_mt5_module import FakeMT5Module


def _make_client(url: str, **overrides):
    mt5 = FakeMT5Module()
    fields = dict(
        url=url, token="test-token", account_id="test-account",
        mt5_client=MT5Client(mt5), mt5_executor=MT5Executor(),
        backoff_base=0.05, backoff_cap=0.2,
    )
    fields.update(overrides)
    return RelayClient(**fields), mt5


def test_auth_success_sends_correct_envelope_and_awaits_ack(client_run):
    peer = FakeCloudPeer(expected_token="test-token", expected_account_id="test-account")
    url = peer.start()
    try:
        client, _mt5 = _make_client(url)
        client_run(client)

        assert peer._connected.wait(timeout=5.0)
        assert peer.received_auth.kind == EnvelopeKind.AUTH
        assert peer.received_auth.payload["token"] == "test-token"
    finally:
        peer.stop()


def test_auth_rejection_raises_auth_failure(client_run):
    peer = FakeCloudPeer(reject_auth=True)
    url = peer.start()
    try:
        client, _mt5 = _make_client(url)
        run = client_run(client)

        with pytest.raises(AuthFailure):
            run.future.result(timeout=5.0)
    finally:
        peer.stop()


def test_auth_failure_close_code_4401_is_detected(client_run):
    peer = FakeCloudPeer(expected_token="right-token")
    url = peer.start()
    try:
        client, _mt5 = _make_client(url, token="wrong-token")
        run = client_run(client)

        with pytest.raises(AuthFailure):
            run.future.result(timeout=5.0)
    finally:
        peer.stop()


def test_dispatch_get_account_equity_calls_mt5_client_via_executor(client_run):
    peer = FakeCloudPeer()
    url = peer.start()
    try:
        client, mt5 = _make_client(url)
        mt5.set_account_info(equity=54_321.0)
        client_run(client)

        response = peer.send_request("get_account_equity", {})

        assert response.kind == EnvelopeKind.RESPONSE
        assert response.payload == {"equity": 54_321.0}
    finally:
        peer.stop()


def test_order_rejection_returns_error_envelope_not_response(client_run):
    peer = FakeCloudPeer()
    url = peer.start()
    try:
        client, mt5 = _make_client(url)
        mt5.set_tick("EURUSD", bid=1.0999, ask=1.1001)
        mt5.script_order_send_result(retcode=10019, comment="Not enough money")
        client_run(client)

        order_payload = {
            "symbol": "EUR/USD", "side": "buy", "order_type": "market", "quantity": 10_000,
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
        }
        response = peer.send_request("place_order", order_payload)

        assert response.kind == EnvelopeKind.ERROR
        assert response.payload["error_type"] == "InsufficientMargin"
    finally:
        peer.stop()


def test_unknown_request_method_returns_error_envelope(client_run):
    peer = FakeCloudPeer()
    url = peer.start()
    try:
        client, _mt5 = _make_client(url)
        client_run(client)

        response = peer.send_request("not_a_real_method", {})

        assert response.kind == EnvelopeKind.ERROR
        assert response.payload["error_type"] == "UnknownMethod"
    finally:
        peer.stop()


def test_subscribe_quotes_tracks_symbol_and_acks(client_run):
    peer = FakeCloudPeer()
    url = peer.start()
    try:
        client, _mt5 = _make_client(url)
        client_run(client)

        response = peer.send_request("subscribe_quotes", {"symbols": ["EUR/USD", "GBP/USD"]})

        assert response.kind == EnvelopeKind.RESPONSE
        assert client.subscribed_symbols == frozenset({"EUR/USD", "GBP/USD"})

        peer.send_request("unsubscribe_quotes", {"symbols": ["EUR/USD"]})
        assert client.subscribed_symbols == frozenset({"GBP/USD"})
    finally:
        peer.stop()


def test_push_quote_and_push_heartbeat_send_event_frames(client_run):
    peer = FakeCloudPeer()
    url = peer.start()
    try:
        client, _mt5 = _make_client(url)
        run = client_run(client)
        assert peer._connected.wait(timeout=5.0)

        snapshot = WireMarketSnapshot(
            symbol="EUR/USD", timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc), bid=1.1, ask=1.1002,
        )
        asyncio.run_coroutine_threadsafe(client.push_quote(snapshot), run.loop).result(timeout=5.0)

        event = peer.read_pushed_event(timeout=5.0)
        assert event.method == "quote"
        assert event.payload["symbol"] == "EUR/USD"

        asyncio.run_coroutine_threadsafe(client.push_heartbeat({"mt5_connected": True}), run.loop).result(timeout=5.0)
        heartbeat = peer.read_pushed_event(timeout=5.0)
        assert heartbeat.method == "heartbeat"
        assert heartbeat.payload == {"mt5_connected": True}
    finally:
        peer.stop()


def test_run_forever_reconnects_with_backoff_after_ordinary_drop(client_run):
    peer = FakeCloudPeer()
    url = peer.start()
    try:
        client, _mt5 = _make_client(url)
        client_run(client)
        assert peer._connected.wait(timeout=5.0)

        peer.force_disconnect()
        peer.wait_for_reconnect(timeout=5.0)  # backoff_base=0.05s keeps this fast
    finally:
        peer.stop()


def test_run_forever_does_not_reconnect_after_auth_failure(client_run):
    peer = FakeCloudPeer(reject_auth=True)
    url = peer.start()
    try:
        client, _mt5 = _make_client(url)
        run = client_run(client)

        with pytest.raises(AuthFailure):
            run.future.result(timeout=5.0)

        # The task must have actually finished (raised), not still be
        # looping in the background waiting to retry.
        assert run.future.done()
    finally:
        peer.stop()
