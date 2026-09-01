from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from relay_protocol.wire_models import WireFill, WireMarketSnapshot, WireOrder, WirePosition


def test_wire_market_snapshot_round_trips_through_json():
    snapshot = WireMarketSnapshot(
        symbol="EUR/USD", timestamp=datetime(2026, 1, 1, 12, 30, tzinfo=timezone.utc),
        bid=1.1000, ask=1.1002,
    )

    restored = WireMarketSnapshot.model_validate_json(snapshot.model_dump_json())

    assert restored == snapshot


def test_wire_order_round_trips_with_enum_fields_as_plain_value_strings():
    order = WireOrder(
        symbol="EUR/USD", side="buy", order_type="market", quantity=10_000,
        stop_loss_price=1.0950, take_profit_price=1.1050,
        status="filled", broker_order_id="mt5-123",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        strategy_name="scalper_v1",
    )

    payload = order.model_dump_json()
    restored = WireOrder.model_validate_json(payload)

    assert restored == order
    # Enum-valued fields must serialize as their .value string, not a
    # Python repr like "OrderSide.BUY" -- a non-fx_bot client (the
    # connector, or a future non-Python one) has no way to parse that.
    assert '"side":"buy"' in payload
    assert '"order_type":"market"' in payload
    assert '"status":"filled"' in payload


def test_wire_order_defaults_match_a_freshly_created_domain_order():
    # Mirrors models.Order's defaults for the fields that have them, so a
    # minimal WireOrder (only the fields an entry order actually sets)
    # converts cleanly without the receiving side inventing values.
    order = WireOrder(
        symbol="GBP/USD", side="sell", order_type="market", quantity=5_000,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert order.time_in_force == "day"
    assert order.status == "pending"
    assert order.exit_reason is None
    assert order.client_order_id is None
    assert order.broker_order_id is None


def test_wire_order_missing_required_field_is_rejected():
    with pytest.raises(ValidationError):
        WireOrder(symbol="EUR/USD", side="buy", order_type="market")  # missing quantity/created_at/updated_at


def test_wire_fill_round_trips():
    fill = WireFill(
        order_client_id="mt5-123", symbol="EUR/USD", side="buy", quantity=10_000,
        price=1.1002, filled_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    restored = WireFill.model_validate_json(fill.model_dump_json())

    assert restored == fill
    assert restored.fees == 0.0


def test_wire_position_round_trips_with_all_optional_fields_populated():
    position = WirePosition(
        symbol="EUR/USD", side="buy", quantity=10_000, avg_entry_price=1.1000,
        stop_price=1.0950, target_price=1.1050, trailing_stop_pips=15,
        opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        strategy_name="scalper_v1", entry_signal_id="sig-1",
        realized_pnl=0.0, max_favorable_excursion=12.5, max_adverse_excursion=-3.0,
        partial_exit_taken=True, swap=-0.42,
    )

    restored = WirePosition.model_validate_json(position.model_dump_json())

    assert restored == position


def test_wire_position_round_trips_with_optional_fields_absent():
    position = WirePosition(
        symbol="EUR/USD", side="sell", quantity=10_000, avg_entry_price=1.1000,
        stop_price=None, target_price=None, trailing_stop_pips=None,
        opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        strategy_name="scalper_v1",
    )

    restored = WirePosition.model_validate_json(position.model_dump_json())

    assert restored == position
    assert restored.entry_signal_id is None
