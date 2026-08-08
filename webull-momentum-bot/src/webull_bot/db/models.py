"""
SQLAlchemy ORM models for Postgres/Supabase, mirroring the tables called out
in the project outline. Deliberately does NOT store every raw tick forever:
`market_observations` holds derived/sampled feature snapshots, not a full
tape. Raw tick/L2 data (once wired up) belongs in a separate high-volume
store if it's kept at all, not this schema.

No Alembic migration is included yet -- for early development,
`scripts/init_db.py` calls `Base.metadata.create_all()` directly. Introduce
Alembic once the schema needs to evolve against data you care about keeping.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _json_type():
    # JSONB on Postgres/Supabase; SQLAlchemy falls back to JSON on other
    # dialects (e.g. SQLite in unit tests) automatically via variant typing.
    return JSONB().with_variant(JSON(), "sqlite")


class Stock(Base):
    __tablename__ = "stocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    company_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    exchange: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sector: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class FloatHistory(Base):
    __tablename__ = "float_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    free_float_shares: Mapped[float] = mapped_column(Float)
    shares_outstanding: Mapped[float] = mapped_column(Float)
    market_cap: Mapped[float | None] = mapped_column(Float, nullable=True)
    float_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    effective_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="massive")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MarketObservation(Base):
    """A derived, sampled feature snapshot -- NOT a raw tick store."""
    __tablename__ = "market_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    last_price: Mapped[float] = mapped_column(Float)
    bid: Mapped[float] = mapped_column(Float)
    ask: Mapped[float] = mapped_column(Float)
    cumulative_volume: Mapped[float] = mapped_column(Float)
    vwap: Mapped[float] = mapped_column(Float)
    high_of_day: Mapped[float] = mapped_column(Float)
    low_of_day: Mapped[float] = mapped_column(Float)
    float_turnover: Mapped[float | None] = mapped_column(Float, nullable=True)
    relative_volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_acceleration: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_acceleration: Mapped[float | None] = mapped_column(Float, nullable=True)
    momentum_ignition_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    candidate_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    extra: Mapped[dict | None] = mapped_column(_json_type(), nullable=True)


class ScannerEvent(Base):
    __tablename__ = "scanner_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    event_type: Mapped[str] = mapped_column(String(64))  # e.g. "discovered", "state_transition"
    from_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class MomentumScoreRecord(Base):
    __tablename__ = "momentum_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    score: Mapped[float] = mapped_column(Float)
    weights_version: Mapped[str] = mapped_column(String(64))
    components: Mapped[dict] = mapped_column(_json_type())


class StrategyVersion(Base):
    __tablename__ = "strategy_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[str] = mapped_column(String(32))
    config: Mapped[dict] = mapped_column(_json_type())
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class SignalRecord(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    action: Mapped[str] = mapped_column(String(32))
    strategy_name: Mapped[str] = mapped_column(String(64))
    strategy_version: Mapped[str] = mapped_column(String(32))
    reference_price: Mapped[float] = mapped_column(Float)
    suggested_stop: Mapped[float | None] = mapped_column(Float, nullable=True)
    suggested_target: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_at_signal: Mapped[float | None] = mapped_column(Float, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(_json_type(), nullable=True)


class OrderRecord(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(16))
    order_type: Mapped[str] = mapped_column(String(16))
    quantity: Mapped[float] = mapped_column(Float)
    limit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    strategy_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), nullable=True)
    trading_mode: Mapped[str] = mapped_column(String(16))  # sandbox/paper/live, for auditability
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class FillRecord(Base):
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(16))
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    filled_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class PositionRecord(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(16))
    quantity: Mapped[float] = mapped_column(Float)
    avg_entry_price: Mapped[float] = mapped_column(Float)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    strategy_name: Mapped[str] = mapped_column(String(64))
    opened_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    max_favorable_excursion: Mapped[float] = mapped_column(Float, default=0.0)
    max_adverse_excursion: Mapped[float] = mapped_column(Float, default=0.0)


class TradeRecord(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    strategy_name: Mapped[str] = mapped_column(String(64))
    side: Mapped[str] = mapped_column(String(16))
    entry_price: Mapped[float] = mapped_column(Float)
    exit_price: Mapped[float] = mapped_column(Float)
    quantity: Mapped[float] = mapped_column(Float)
    opened_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    closed_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    exit_reason: Mapped[str] = mapped_column(String(32))
    pnl: Mapped[float] = mapped_column(Float)
    pnl_pct: Mapped[float] = mapped_column(Float)
    max_favorable_excursion: Mapped[float] = mapped_column(Float)
    max_adverse_excursion: Mapped[float] = mapped_column(Float)
    trading_mode: Mapped[str] = mapped_column(String(16))


class RiskEventRecord(Base):
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    reason: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict | None] = mapped_column(_json_type(), nullable=True)


class BacktestResult(Base):
    __tablename__ = "backtest_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy_name: Mapped[str] = mapped_column(String(64))
    strategy_version: Mapped[str] = mapped_column(String(32))
    mis_weights_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    start_date: Mapped[datetime] = mapped_column(DateTime)
    end_date: Mapped[datetime] = mapped_column(DateTime)
    total_trades: Mapped[int] = mapped_column(Integer)
    win_rate: Mapped[float] = mapped_column(Float)
    total_pnl: Mapped[float] = mapped_column(Float)
    max_drawdown: Mapped[float] = mapped_column(Float)
    sharpe: Mapped[float | None] = mapped_column(Float, nullable=True)
    params: Mapped[dict | None] = mapped_column(_json_type(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PerformanceStat(Base):
    __tablename__ = "performance_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period_start: Mapped[datetime] = mapped_column(DateTime, index=True)
    period_end: Mapped[datetime] = mapped_column(DateTime, index=True)
    scope: Mapped[str] = mapped_column(String(64))  # e.g. "daily", "strategy:momentum_breakout"
    trades: Mapped[int] = mapped_column(Integer)
    win_rate: Mapped[float] = mapped_column(Float)
    total_pnl: Mapped[float] = mapped_column(Float)
    avg_r_multiple: Mapped[float | None] = mapped_column(Float, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(_json_type(), nullable=True)


class MomentumEventRecord(Base):
    """
    Traded AND non-traded momentum events, with forward-looking outcome
    windows. This is the core data-collection artifact for improving the
    MIS formula and entry strategies offline.
    """
    __tablename__ = "momentum_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    trigger_reason: Mapped[str] = mapped_column(String(128))
    was_traded: Mapped[bool] = mapped_column(Boolean, default=False)
    score_at_event: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_at_event: Mapped[float] = mapped_column(Float)
    metrics_at_event: Mapped[dict | None] = mapped_column(_json_type(), nullable=True)

    outcome_30s: Mapped[dict | None] = mapped_column(_json_type(), nullable=True)
    outcome_1m: Mapped[dict | None] = mapped_column(_json_type(), nullable=True)
    outcome_3m: Mapped[dict | None] = mapped_column(_json_type(), nullable=True)
    outcome_5m: Mapped[dict | None] = mapped_column(_json_type(), nullable=True)
    outcome_10m: Mapped[dict | None] = mapped_column(_json_type(), nullable=True)
    outcome_15m: Mapped[dict | None] = mapped_column(_json_type(), nullable=True)

    max_favorable_excursion_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_adverse_excursion_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    hod_broken: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    vwap_failed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    outcome_label: Mapped[str] = mapped_column(String(16), default="unknown")
