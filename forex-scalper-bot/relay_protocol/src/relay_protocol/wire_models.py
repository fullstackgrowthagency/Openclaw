"""
JSON-safe mirrors of fx_bot's `models.py` dataclasses (`MarketSnapshot`,
`Order`, `Position`, `Fill`), field-for-field, sent inside an
`Envelope.payload`.

Deliberately NOT importing fx_bot's Enum classes (`OrderSide`,
`OrderType`, etc.) -- this package must stay importable from the
Windows-only connector process without pulling in `fx_bot` at all (see
this module's package docstring). Enum-valued fields are therefore plain
`str`, holding exactly the `.value` a matching fx_bot enum member would
produce (e.g. `side="buy"`, never `side="BUY"` or `side="OrderSide.BUY"`)
-- fx_bot's own conversion code (in `brokers/local_connector/`, added in
Phase 5b) is responsible for validating those strings by round-tripping
them through its real enums via e.g. `OrderSide(wire_order.side)`, which
raises `ValueError` on anything unrecognized. Keeping validation there
rather than here also means this package's whitelist of legal values
never has to be kept in sync with fx_bot's as that enum grows.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class WireMarketSnapshot(BaseModel):
    symbol: str
    timestamp: datetime
    bid: float
    ask: float


class WireOrder(BaseModel):
    symbol: str
    side: str  # OrderSide.value: "buy" | "sell"
    order_type: str  # OrderType.value
    quantity: float
    time_in_force: str = "day"  # TimeInForce.value
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    trailing_pips: Optional[float] = None
    exit_reason: Optional[str] = None  # ExitReason.value, closing orders only
    status: str = "pending"  # OrderStatus.value
    client_order_id: Optional[str] = None
    broker_order_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    strategy_name: Optional[str] = None
    signal_id: Optional[str] = None


class WireFill(BaseModel):
    order_client_id: str
    symbol: str
    side: str  # OrderSide.value
    quantity: float
    price: float
    filled_at: datetime
    fees: float = 0.0


class WirePosition(BaseModel):
    symbol: str
    side: str  # OrderSide.value
    quantity: float
    avg_entry_price: float
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    trailing_stop_pips: Optional[float] = None
    opened_at: datetime
    strategy_name: str
    entry_signal_id: Optional[str] = None
    realized_pnl: float = 0.0
    max_favorable_excursion: float = 0.0
    max_adverse_excursion: float = 0.0
    partial_exit_taken: bool = False
    swap: float = 0.0
