"""
In-memory domain models shared across scanner, strategy, risk, execution,
and data-collection layers. These are plain dataclasses -- persistence
mapping lives separately in db/models.py so the trading logic never depends
on the ORM.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .enums import (
    CandidateState,
    ExitReason,
    MomentumOutcome,
    OrderSide,
    OrderStatus,
    OrderType,
    SignalAction,
    TimeInForce,
)


@dataclass
class FloatData:
    symbol: str
    free_float_shares: float
    shares_outstanding: float
    market_cap: Optional[float]
    float_percent: Optional[float]           # free_float / shares_outstanding
    effective_date: Optional[datetime]        # as-of date reported by the provider
    fetched_at: datetime
    source: str = "massive"


@dataclass
class MarketSnapshot:
    """A single point-in-time read of a symbol's tape/quote state."""
    symbol: str
    timestamp: datetime
    last_price: float
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    cumulative_volume: float
    vwap: float
    high_of_day: float
    low_of_day: float
    open_price: float
    premarket_high: Optional[float] = None
    prev_close: Optional[float] = None


@dataclass
class MomentumMetrics:
    """Derived metrics computed from a rolling window of MarketSnapshots + FloatData."""
    symbol: str
    timestamp: datetime

    float_turnover: float             # cumulative_volume / free_float
    float_velocity_1m: float          # volume in last 1m / free_float
    float_velocity_3m: float
    float_velocity_5m: float

    relative_volume: float            # current volume vs typical volume at this time of day
    volume_accel_1m_3m: float         # recent 1m volume rate vs preceding 3m average rate

    price_velocity_1m: float          # % price change over window
    price_velocity_3m: float
    price_velocity_5m: float
    price_velocity_15m: float
    price_acceleration: float         # change in price velocity itself

    vwap: float
    distance_from_vwap_pct: float
    distance_from_hod_pct: float
    distance_from_premarket_high_pct: Optional[float]
    distance_from_resistance_pct: Optional[float]

    spread_abs: float
    spread_pct: float
    dollar_volume: float
    trade_velocity: Optional[float] = None  # trades/sec, once tick data is wired up


@dataclass
class MomentumScoreComponents:
    """Each sub-score is normalized to 0-100 before weighting in the MIS calculator."""
    float_score: float
    float_velocity_score: float
    relative_volume_score: float
    volume_acceleration_score: float
    price_acceleration_score: float
    breakout_proximity_score: float
    trend_quality_score: float
    liquidity_score: float


@dataclass
class MomentumScore:
    symbol: str
    timestamp: datetime
    score: float  # 0-100 final Momentum Ignition Score
    components: MomentumScoreComponents
    weights_version: str


@dataclass
class Candidate:
    symbol: str
    state: CandidateState
    discovered_at: datetime
    last_updated_at: datetime
    float_data: Optional[FloatData] = None
    latest_metrics: Optional[MomentumMetrics] = None
    latest_score: Optional[MomentumScore] = None
    last_price: Optional[float] = None  # set from the latest snapshot seen by CandidateWatcher.update(); None until its first tick
    resistance_level: Optional[float] = None
    # Static levels from volume-profile analysis at discovery time (see
    # scanner/broad_scanner.py's _compute_static_resistance_levels and
    # metrics/volume_profile.py) -- high-volume-node price levels that
    # existed before this candidate started being tracked. Merged with the
    # running intraday high in candidate_watcher.py's update_resistance:
    # the nearest one of these still above the running high becomes the
    # active resistance_level, rather than resistance_level being purely
    # the running high of day.
    static_resistance_levels: list[float] = field(default_factory=list)
    breakout_price: Optional[float] = None      # price at which the initial breakout occurred
    pullback_low: Optional[float] = None        # for breakout-pullback strategy tracking
    state_history: list[tuple[CandidateState, datetime]] = field(default_factory=list)
    notes: str = ""


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
    score_at_signal: Optional[float] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class RiskDecision:
    approved: bool
    reason: str
    max_shares: Optional[int] = None
    risk_amount: Optional[float] = None


@dataclass
class Order:
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: float
    time_in_force: TimeInForce = TimeInForce.DAY
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
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
    trailing_stop_pct: Optional[float]
    opened_at: datetime
    strategy_name: str
    entry_signal_id: Optional[str] = None
    realized_pnl: float = 0.0
    max_favorable_excursion: float = 0.0
    max_adverse_excursion: float = 0.0


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


@dataclass
class RiskEvent:
    event_type: str
    symbol: Optional[str]
    timestamp: datetime
    reason: str
    metadata: dict = field(default_factory=dict)


@dataclass
class MomentumEvent:
    """
    A meaningful momentum observation, recorded regardless of whether the
    bot traded it. This is the core artifact for the data-collection /
    strategy-improvement loop described in the project outline.
    """
    symbol: str
    detected_at: datetime
    trigger_reason: str                 # e.g. "score_crossed_armed_threshold", "breakout_confirmed"
    was_traded: bool
    score_at_event: Optional[float]
    metrics_at_event: Optional[MomentumMetrics]
    price_at_event: float

    # Forward-looking outcome snapshots, filled in asynchronously as time passes.
    outcome_30s: Optional[dict] = None
    outcome_1m: Optional[dict] = None
    outcome_3m: Optional[dict] = None
    outcome_5m: Optional[dict] = None
    outcome_10m: Optional[dict] = None
    outcome_15m: Optional[dict] = None

    max_favorable_excursion_pct: Optional[float] = None
    max_adverse_excursion_pct: Optional[float] = None
    hod_broken: Optional[bool] = None
    vwap_failed: Optional[bool] = None
    outcome_label: MomentumOutcome = MomentumOutcome.UNKNOWN
