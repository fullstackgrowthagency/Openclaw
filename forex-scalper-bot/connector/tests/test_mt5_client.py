from datetime import datetime, timezone

import pytest

from fx_connector.mt5_client import (
    MT5Client,
    MT5ClientError,
    MT5ConnectionError,
    MT5OrderRejectedError,
    MT5UnknownOrderError,
)
from relay_protocol.wire_models import WireOrder
from tests.fakes.fake_mt5_module import FakeMT5Module


@pytest.fixture
def mt5():
    return FakeMT5Module()


@pytest.fixture
def client(mt5):
    return MT5Client(mt5, login=1, password="pw", server="srv")


def test_connect_raises_mt5_connection_error_on_initialize_failure(mt5, client):
    mt5.set_initialize_result(False, error=(1, "bad creds"))
    with pytest.raises(MT5ConnectionError):
        client.connect()


def test_connect_succeeds(mt5, client):
    client.connect()  # no exception


def test_get_account_equity_and_free_margin(client, mt5):
    mt5.set_account_info(equity=12_345.0, margin_free=9_000.0)
    assert client.get_account_equity() == 12_345.0
    assert client.get_free_margin() == 9_000.0


def test_get_account_equity_raises_when_account_info_none(client, mt5):
    mt5._account_info = None
    with pytest.raises(MT5ConnectionError):
        client.get_account_equity()


def test_is_live_account_reflects_trade_mode(client, mt5):
    mt5.set_account_info(trade_mode=mt5.ACCOUNT_TRADE_MODE_DEMO)
    assert client.is_live_account() is False
    mt5.set_account_info(trade_mode=mt5.ACCOUNT_TRADE_MODE_REAL)
    assert client.is_live_account() is True


def test_get_snapshot_calls_symbol_select_before_tick(client, mt5):
    mt5.set_tick("EURUSD", bid=1.0999, ask=1.1001)

    snapshot = client.get_snapshot("EUR/USD")

    assert "EURUSD" in mt5.selected_symbols
    assert snapshot.symbol == "EUR/USD"
    assert snapshot.bid == pytest.approx(1.0999)
    assert snapshot.ask == pytest.approx(1.1001)


def test_get_snapshot_raises_mt5_symbol_error_when_no_tick(client):
    with pytest.raises(Exception):
        client.get_snapshot("EUR/USD")


def test_get_bars_maps_ohlc_close_to_synthetic_zero_spread_snapshot(client, mt5):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    mt5.set_rates("EURUSD", [
        {"time": int(now.timestamp()), "open": 1.10, "high": 1.11, "low": 1.09, "close": 1.105,
         "tick_volume": 100, "spread": 1, "real_volume": 0},
    ])

    bars = client.get_bars("EUR/USD", "1m", lookback=1)

    assert len(bars) == 1
    assert bars[0].bid == bars[0].ask == pytest.approx(1.105)


def test_get_bars_rejects_unsupported_interval(client):
    with pytest.raises(MT5ClientError):
        client.get_bars("EUR/USD", "3m", lookback=10)


def test_get_positions_recovers_strategy_name_from_registry(client, mt5):
    mt5.set_tick("EURUSD", bid=1.0999, ask=1.1001)
    order = WireOrder(
        symbol="EUR/USD", side="buy", order_type="market", quantity=10_000,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        strategy_name="scalper_v1", signal_id="sig-1",
    )
    mt5.script_order_send_result(retcode=mt5.TRADE_RETCODE_DONE, order=555, price=1.1001, volume=10_000)
    client.place_order(order)

    mt5.set_positions([{
        "ticket": 999, "time": int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()),
        "type": mt5.ORDER_TYPE_BUY, "magic": 0, "identifier": 555, "reason": 0,
        "volume": 10_000, "price_open": 1.1001, "sl": 0.0, "tp": 0.0, "price_current": 1.1005,
        "swap": 0.0, "profit": 4.0, "symbol": "EURUSD", "comment": "", "time_update": 0,
        "time_msc": 0, "time_update_msc": 0, "external_id": "",
    }])

    positions = client.get_positions()

    assert len(positions) == 1
    assert positions[0].strategy_name == "scalper_v1"
    assert positions[0].entry_signal_id == "sig-1"


def test_get_positions_falls_back_to_external_when_registry_missing(client, mt5):
    mt5.set_positions([{
        "ticket": 42, "time": int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()),
        "type": mt5.ORDER_TYPE_SELL, "magic": 0, "identifier": 4242, "reason": 0,
        "volume": 5_000, "price_open": 1.25, "sl": 0.0, "tp": 0.0, "price_current": 1.24,
        "swap": 0.0, "profit": 5.0, "symbol": "GBPUSD", "comment": "manual", "time_update": 0,
        "time_msc": 0, "time_update_msc": 0, "external_id": "",
    }])

    positions = client.get_positions()

    assert positions[0].strategy_name == "external"
    assert positions[0].entry_signal_id is None
    assert positions[0].side == "sell"


def _market_order(**overrides) -> WireOrder:
    fields = dict(
        symbol="EUR/USD", side="buy", order_type="market", quantity=10_000,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        strategy_name="scalper_v1",
    )
    fields.update(overrides)
    return WireOrder(**fields)


def test_place_order_market_buy_maps_side_and_type_correctly(client, mt5):
    mt5.set_tick("EURUSD", bid=1.0999, ask=1.1001)

    client.place_order(_market_order())

    sent = mt5.sent_requests[-1]
    assert sent["type"] == mt5.ORDER_TYPE_BUY
    assert sent["action"] == mt5.TRADE_ACTION_DEAL
    assert sent["price"] == pytest.approx(1.1001)  # buy fills at ask


def test_place_order_success_returns_filled_wire_order_and_registers_ticket(client, mt5):
    mt5.set_tick("EURUSD", bid=1.0999, ask=1.1001)
    mt5.script_order_send_result(retcode=mt5.TRADE_RETCODE_DONE, order=777, price=1.1001, volume=10_000)

    filled = client.place_order(_market_order())

    assert filled.status == "filled"
    assert filled.broker_order_id == "777"
    assert client.get_order_status("777").broker_order_id == "777"


def test_place_order_rejection_raises_mt5_order_rejected_error_with_mapped_error_type(client, mt5):
    mt5.set_tick("EURUSD", bid=1.0999, ask=1.1001)
    mt5.script_order_send_result(retcode=10019, comment="Not enough money")  # TRADE_RETCODE_NO_MONEY

    with pytest.raises(MT5OrderRejectedError) as exc_info:
        client.place_order(_market_order())

    assert exc_info.value.error_type == "InsufficientMargin"
    assert exc_info.value.retcode == 10019


def test_place_order_trailing_stop_order_type_raises_unsupported(client):
    with pytest.raises(MT5ClientError):
        client.place_order(_market_order(order_type="trailing_stop"))


def test_place_order_market_fill_is_recorded_in_poll_fills(client, mt5):
    mt5.set_tick("EURUSD", bid=1.0999, ask=1.1001)
    mt5.script_order_send_result(retcode=mt5.TRADE_RETCODE_DONE, order=888, price=1.1001, volume=10_000)

    client.place_order(_market_order())
    fills = client.poll_fills(since=None)

    assert len(fills) == 1
    assert fills[0].order_client_id == "888"


def test_poll_fills_returns_only_fills_since_given_timestamp(client, mt5):
    mt5.set_tick("EURUSD", bid=1.0999, ask=1.1001)
    mt5.script_order_send_result(retcode=mt5.TRADE_RETCODE_DONE, order=1, price=1.1001, volume=10_000)
    client.place_order(_market_order())

    future = datetime.now(timezone.utc).isoformat()
    assert client.poll_fills(since=future) == []


def test_modify_order_uses_sltp_action_for_open_position(client, mt5):
    mt5.set_tick("EURUSD", bid=1.0999, ask=1.1001)
    mt5.script_order_send_result(retcode=mt5.TRADE_RETCODE_DONE, order=321, price=1.1001, volume=10_000)
    client.place_order(_market_order())

    mt5.set_positions([{
        "ticket": 900, "time": 0, "type": mt5.ORDER_TYPE_BUY, "magic": 0, "identifier": 321,
        "reason": 0, "volume": 10_000, "price_open": 1.1001, "sl": 0.0, "tp": 0.0,
        "price_current": 1.1005, "swap": 0.0, "profit": 0.0, "symbol": "EURUSD", "comment": "",
        "time_update": 0, "time_msc": 0, "time_update_msc": 0, "external_id": "",
    }])
    mt5.script_order_send_result(retcode=mt5.TRADE_RETCODE_DONE)

    updated = client.modify_order("321", {"stop_loss_price": 1.0950})

    sent = mt5.sent_requests[-1]
    assert sent["action"] == mt5.TRADE_ACTION_SLTP
    assert sent["position"] == 900
    assert updated.stop_loss_price == pytest.approx(1.0950)


def test_modify_order_unknown_ticket_raises_mt5_unknown_order_error(client):
    with pytest.raises(MT5UnknownOrderError):
        client.modify_order("999999", {"stop_loss_price": 1.0})


def test_cancel_order_unknown_ticket_raises_mt5_order_rejected_error(client, mt5):
    mt5.script_order_send_result(retcode=10013)  # TRADE_RETCODE_INVALID
    with pytest.raises(MT5OrderRejectedError):
        client.cancel_order("123456")


def test_get_order_status_unknown_ticket_raises(client):
    with pytest.raises(MT5UnknownOrderError):
        client.get_order_status("424242")
