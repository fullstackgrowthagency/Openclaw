"""
In-memory domain models, mirroring webull-momentum-bot/src/webull_bot/models.py's
role: plain dataclasses shared across the strategy/risk/execution layers,
independent of any persistence mapping. Forex-specific differences from the
equities bot's shapes:

- MarketSnapshot is bid/ask-based (no single "last trade price," no
  cumulative_volume/VWAP/high-of-day -- those are exchange/tape concepts
  that don't exist the same way in a decentralized OTC market fed by a
  single broker's quotes).
- Position/Trade carry `swap` (overnight financing) -- a real forex
  concept with no equities-bot equivalent.
- Order carries `stop_loss_price`/`take_profit_price` directly, matching
  MT4/5's bracket-on-open convention (attach SL/TP to the order itself)
  rather than only the equities bot's separate resting-bracket-order
  pattern.
- RiskDecision.max_units, not max_shares.
- No MomentumMetrics/Candidate/MomentumEvent equivalents yet -- those
  belong to the scanner/rule-builder phase once there's a candidate
  concept to attach them to.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .enums import ExitReason, OrderSide, OrderStatus, OrderType, SignalAction, TimeInForce
from .pairs import price_diff_to_pips


@dataclass
class MarketSnapshot:
    """A single point-in-time bid/ask read for a pair."""
    symbol: str  # "EUR/USD" form -- see pairs.py
    timestamp: datetime
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_pips(self) -> float:
        return price_diff_to_pips(self.symbol, self.ask - self.bid)


@dataclass
class Signal:
    symbol: str
    action: SignalAction
    generated_at: datetime
    strategy_name: str
    strategy_version: str
    reference_price: float
    suggested_stop: Optional[float] = None
    suggested_target: Optional[float] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class RiskDecision:
    approved: bool
    reason: str
    max_units: Optional[float] = None
    risk_amount: Optional[float] = None


@dataclass
class Order:
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: float  # units of base currency, e.g. 100_000 == 1 standard lot
    time_in_force: TimeInForce = TimeInForce.DAY
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    # Attached directly to the order, matching MT4/5's bracket-on-open
    # convention -- most retail scalping strategies want SL/TP set the
    # instant the order fills, not placed as separate resting orders
    # afterward (contrast webull_bot's OCO-bracket-after-fill pattern).
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    trailing_pips: Optional[float] = None
    status: OrderStatus = OrderStatus.PENDING
    client_order_id: Optional[str] = None
    broker_order_id: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    strategy_name: Optional[str] = None
    signal_id: Optional[str] = None


@dataclass
class Fill:
    order_client_id: str
    symbol: str
    side: OrderSide
    quantity: float
    price: float
    filled_at: datetime
    fees: float = 0.0


@dataclass
class Position:
    symbol: str
    side: OrderSide
    quantity: float
    avg_entry_price: float
    stop_price: Optional[float]
    target_price: Optional[float]
    trailing_stop_pips: Optional[float]
    opened_at: datetime
    strategy_name: str
    entry_signal_id: Optional[str] = None
    realized_pnl: float = 0.0
    max_favorable_excursion: float = 0.0
    max_adverse_excursion: float = 0.0
    partial_exit_taken: bool = False
    # Accumulated overnight financing/rollover charge -- 0.0 for a
    # position never held across a rollover, which is the common case for
    # a scalping bot, but real and worth tracking accurately when it does
    # happen rather than silently dropping it from P&L.
    swap: float = 0.0


@dataclass
class Trade:
    symbol: str
    strategy_name: str
    side: OrderSide
    entry_price: float
    exit_price: float
    quantity: float
    opened_at: datetime
    closed_at: datetime
    exit_reason: ExitReason
    pnl: float
    pnl_pct: float
    max_favorable_excursion: float
    max_adverse_excursion: float
    swap: float = 0.0


@dataclass
class RiskEvent:
    event_type: str
    symbol: Optional[str]
    timestamp: datetime
    reason: str
    metadata: dict = field(default_factory=dict)
