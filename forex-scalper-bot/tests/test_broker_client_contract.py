"""
Shared BrokerClient-contract suite -- the same behavioral cases run
against both PaperBrokerClient and LocalConnectorBroker (backed by a
real RelayServer + FakeRelayPeer, see tests/fakes/fake_relay_peer.py),
proving the two backends are actually interchangeable from a caller's
point of view, not just independently self-consistent.

Deliberately tests the ABC's observable SHAPE only -- a fixed, canned
fill price for the local-connector harness, not a reimplementation of
PaperBrokerClient's own spread-crossing fill math (that's already
covered by test_paper_broker_client.py). `poll_fills(since=...)`'s
time-filtering behavior is paper-only for the same reason: real
filtering-by-time for a connector would happen on the MT5/connector side
itself (not built until Phase 5d+), and this suite's static per-method
FakeRelayPeer scripts can't express "different reply depending on the
request payload" without reimplementing that logic here too.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fx_bot.brokers.local_connector.broker import LocalConnectorBroker
from fx_bot.brokers.local_connector.relay_server import RelayServer
from fx_bot.brokers.paper.client import PaperBrokerClient
from fx_bot.enums import OrderSide, OrderStatus, OrderType
from fx_bot.models import MarketSnapshot, Order
from tests.conftest import TEST_ACCOUNT_ID, TEST_TOKEN, _test_authenticator
from tests.fakes.fake_relay_peer import FakeRelayPeer


class _PaperHarness:
    def __init__(self):
        self.broker = PaperBrokerClient()

    def prime_snapshot(self, symbol="EUR/USD", bid=1.0999, ask=1.1001, when=None):
        snapshot = MarketSnapshot(symbol=symbol, timestamp=when or datetime(2026, 1, 1), bid=bid, ask=ask)
        self.broker.feed_snapshot(snapshot)
        return snapshot

    def script_successful_fill(self, order: Order, **_ignored) -> None:
        pass  # PaperBrokerClient fills for real; nothing to pre-script.

    def script_account_equity(self, equity: float = 10_000.0) -> None:
        pass  # PaperBrokerClient already defaults to this.

    def teardown(self) -> None:
        pass


class _LocalConnectorHarness:
    def __init__(self):
        self.server = RelayServer(authenticator=_test_authenticator, auth_grace_seconds=5.0)
        self.server.start()
        self.peer = FakeRelayPeer()
        self.peer.start(f"ws://{self.server.host}:{self.server.port}")
        self.peer.send_auth(TEST_TOKEN, TEST_ACCOUNT_ID)
        connection = self.server.accept(timeout=5.0)
        self.broker = LocalConnectorBroker(connection)

    def prime_snapshot(self, symbol="EUR/USD", bid=1.0999, ask=1.1001, when=None):
        when = when or datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.peer.script_response(
            "get_snapshot", {"symbol": symbol, "timestamp": when.isoformat(), "bid": bid, "ask": ask},
        )
        return MarketSnapshot(symbol=symbol, timestamp=when, bid=bid, ask=ask)

    def script_successful_fill(
        self, order: Order, *, fill_price: float = 1.1001, broker_order_id: str = "mt5-1", when: datetime | None = None,
    ) -> None:
        when = when or datetime(2026, 1, 1, tzinfo=timezone.utc)
        wire_order = {
            "symbol": order.symbol, "side": order.side.value, "order_type": order.order_type.value,
            "quantity": order.quantity, "time_in_force": order.time_in_force.value,
            "limit_price": order.limit_price, "stop_price": order.stop_price,
            "stop_loss_price": order.stop_loss_price, "take_profit_price": order.take_profit_price,
            "trailing_pips": order.trailing_pips, "exit_reason": None, "status": "filled",
            "client_order_id": order.client_order_id, "broker_order_id": broker_order_id,
            "created_at": when.isoformat(), "updated_at": when.isoformat(),
            "strategy_name": order.strategy_name, "signal_id": order.signal_id,
        }
        self.peer.script_response("place_order", wire_order)
        self.peer.script_response("get_order_status", wire_order)
        self.peer.script_response("get_positions", {"positions": [{
            "symbol": order.symbol, "side": order.side.value, "quantity": order.quantity,
            "avg_entry_price": fill_price, "stop_price": None, "target_price": None,
            "trailing_stop_pips": None, "opened_at": when.isoformat(),
            "strategy_name": order.strategy_name or "", "entry_signal_id": None,
            "realized_pnl": 0.0, "max_favorable_excursion": 0.0, "max_adverse_excursion": 0.0,
            "partial_exit_taken": False, "swap": 0.0,
        }]})
        self.peer.script_response("poll_fills", {"fills": [{
            "order_client_id": broker_order_id, "symbol": order.symbol, "side": order.side.value,
            "quantity": order.quantity, "price": fill_price, "filled_at": when.isoformat(), "fees": 0.0,
        }]})

    def script_account_equity(self, equity: float = 10_000.0) -> None:
        self.peer.script_response("get_account_equity", {"equity": equity})

    def teardown(self) -> None:
        self.peer.stop()
        self.server.stop()


@pytest.fixture(params=["paper", "local_connector"])
def broker_harness(request):
    harness = _PaperHarness() if request.param == "paper" else _LocalConnectorHarness()
    try:
        yield harness
    finally:
        harness.teardown()


def _entry_order() -> Order:
    return Order(symbol="EUR/USD", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=10_000)


def test_place_market_order_returns_order_with_broker_order_id_and_terminal_status(broker_harness):
    broker_harness.prime_snapshot()
    order = _entry_order()
    broker_harness.script_successful_fill(order)

    filled = broker_harness.broker.place_order(order)

    assert filled.broker_order_id is not None
    assert filled.status == OrderStatus.FILLED


def test_get_positions_reflects_a_just_filled_order(broker_harness):
    broker_harness.prime_snapshot()
    order = _entry_order()
    broker_harness.script_successful_fill(order)
    broker_harness.broker.place_order(order)

    positions = broker_harness.broker.get_positions()

    assert len(positions) == 1
    assert positions[0].symbol == "EUR/USD"
    assert positions[0].quantity == 10_000


def test_poll_fills_returns_the_fill_for_a_placed_order(broker_harness):
    broker_harness.prime_snapshot()
    order = _entry_order()
    broker_harness.script_successful_fill(order)
    broker_harness.broker.place_order(order)

    fills = broker_harness.broker.poll_fills()

    assert len(fills) == 1
    assert fills[0].symbol == "EUR/USD"
    assert fills[0].side == OrderSide.BUY


def test_get_snapshot_returns_the_primed_snapshot(broker_harness):
    broker_harness.prime_snapshot(symbol="EUR/USD", bid=1.0999, ask=1.1001)

    result = broker_harness.broker.get_snapshot("EUR/USD")

    assert result.symbol == "EUR/USD"
    assert result.bid == pytest.approx(1.0999)
    assert result.ask == pytest.approx(1.1001)


def test_get_account_equity_returns_a_positive_number(broker_harness):
    broker_harness.script_account_equity(10_000.0)

    assert broker_harness.broker.get_account_equity() > 0


def test_get_order_status_returns_the_same_order_after_placing(broker_harness):
    broker_harness.prime_snapshot()
    order = _entry_order()
    broker_harness.script_successful_fill(order)
    filled = broker_harness.broker.place_order(order)

    status = broker_harness.broker.get_order_status(filled.broker_order_id)

    assert status.symbol == "EUR/USD"
    assert status.side == OrderSide.BUY


def test_is_live_is_a_bool(broker_harness):
    assert isinstance(broker_harness.broker.is_live, bool)
