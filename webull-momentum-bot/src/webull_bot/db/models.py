"""
SQLAlchemy ORM models for Postgres/Supabase, mirroring the tables called out
in the project outline. Deliberately does NOT store every raw tick forever:
`market_observations` holds derived/sampled feature snapshots, not a full
tape. Raw tick/L2 data (once wired up) belongs in a separate high-volume
store if it's kept at all, not this schema.

No Alembic migration is included yet -- for early development,
`scripts/init_db.py` calls `Base.metadata.create_all()` directly. Introduce
Alembic once the schema needs to evolve against data you care about keeping.

`create_all()` (in `db/session.py`) only creates a table that doesn't
exist yet -- it silently does nothing to a table that's already there,
even after a model below gains new columns. Real incident this caused
(2026-08-11): the VPS's long-lived `trades` table predated
`TradeRecord.max_favorable_excursion`/`max_adverse_excursion`/
`trading_mode`, so every `record_trade()` call had been failing (and
getting silently swallowed by the caller's `except Exception:
logger.exception(...)`) since those columns were added -- the dashboard's
Performance/Trade History cards stayed empty despite real trades closing.
`db/session.py`'s `create_all()` now also runs `sync_schema()` right
after, which diffs each already-existing table's live columns against
this file's models and `ALTER TABLE ... ADD COLUMN`s in whatever's
missing (see that function's docstring for exactly what it does and
doesn't cover) -- but it's still not a real migration tool, only a cheap
patch for the specific "silently broken INSERT" failure mode above.
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
    UniqueConstraint,
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
    # Nullable (2026-08-15 multi-tenant conversion): scanning/discovery now
    # happens per-user (one TradingLoop per connected account -- see
    # runtime/loop_registry.py), so this is who that discovery belongs to.
    # Nullable rather than NOT NULL because existing single-tenant rows
    # (written before this column existed) have no user to backfill to
    # automatically -- see docs/ARCHITECTURE.md's "Multi-tenant auth"
    # section for the production cutover that assigns them to the
    # operator's own account.
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    # Same nullable-for-backfill rationale as user_id above (2026-08-15
    # multi-bot framework): which of that user's bots this event belongs
    # to -- see db/models.py's Bot class.
    bot_id: Mapped[int | None] = mapped_column(ForeignKey("bots.id"), nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    event_type: Mapped[str] = mapped_column(String(64))  # e.g. "discovered", "state_transition"
    from_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class MomentumScoreRecord(Base):
    __tablename__ = "momentum_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # See ScannerEvent.user_id's comment -- same nullable-for-backfill
    # rationale.
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    bot_id: Mapped[int | None] = mapped_column(ForeignKey("bots.id"), nullable=True, index=True)
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
    # See ScannerEvent.user_id's comment -- same nullable-for-backfill
    # rationale. client_order_id stays the natural key for the upsert in
    # record_order (UUIDs, so no realistic cross-user collision), but
    # every lookup is still scoped by user_id too -- see repository.py.
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    bot_id: Mapped[int | None] = mapped_column(ForeignKey("bots.id"), nullable=True, index=True)
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
    """Upsert-based snapshot of currently-open positions (2026-08-20
    restart-blind-spot fix) -- one row per (user_id, bot_id, symbol) while
    a position is open, deleted the moment it closes (via any path:
    _finalize_exit, reconcile's confirmed-missing drop, or a full
    kill-switch/manual/EOD-flatten close -- all of those route through one
    of those two). This is NOT a historical ledger (see TradeRecord for
    that) -- closed_at/realized_pnl below predate this repurposing and are
    legacy/unused by the new write path (upsert_open_position/
    delete_open_position in db/repository.py), kept only because
    sync_schema() only adds columns, never drops them, and no row had ever
    existed in this table before this repurposing.

    Written only on lifecycle transitions (entry filled, partial exit
    quantity change, adoption from broker) -- NOT on every tick's MFE/MAE
    update -- to keep this off TradingLoop's hot path (see
    TradingLoop.get_last_known_price's docstring for the same rate-limit-
    pressure rationale applied elsewhere in this codebase).
    max_favorable_excursion/max_adverse_excursion are therefore
    intentionally NOT tracked here; a Position rebuilt from this table
    always starts those at 0.0 -- the same "best-effort, not exact"
    tradeoff _build_trade_for_external_close already accepts for
    exit_price.

    Read once at process startup by reconcile_positions_from_broker's
    first call (see TradingLoop._load_position_snapshot), specifically to
    detect a position that closed at the broker WHILE this process was
    down/restarting -- see that method's docstring. sync_schema() is
    additive-only and won't retroactively add the UniqueConstraint below
    to an already-existing `positions` table, so db/repository.py's
    upsert/delete functions use query-then-insert-or-update (like
    record_order already does) rather than relying on it firing."""
    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("user_id", "bot_id", "symbol", name="uq_positions_user_bot_symbol"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # See ScannerEvent.user_id's comment -- same nullable-for-backfill
    # rationale.
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    bot_id: Mapped[int | None] = mapped_column(ForeignKey("bots.id"), nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(16))
    quantity: Mapped[float] = mapped_column(Float)
    avg_entry_price: Mapped[float] = mapped_column(Float)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    strategy_name: Mapped[str] = mapped_column(String(64))
    opened_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    # Best-effort external-close cleanup only (see reconcile's
    # stale_position cleanup branch) -- None for anything adopted before
    # broker-side bracket support existed, or a broker that doesn't
    # support resting orders; harmless either way, cleanup is just
    # skipped.
    broker_stop_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    broker_target_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # legacy, unused
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)  # legacy, unused
    max_favorable_excursion: Mapped[float] = mapped_column(Float, default=0.0)  # legacy, unused
    max_adverse_excursion: Mapped[float] = mapped_column(Float, default=0.0)  # legacy, unused


class TradeRecord(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # See ScannerEvent.user_id's comment -- same nullable-for-backfill
    # rationale.
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    bot_id: Mapped[int | None] = mapped_column(ForeignKey("bots.id"), nullable=True, index=True)
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


class User(Base):
    """A dashboard account (2026-08-15 multi-tenant conversion). One user
    connects one Webull account (see BrokerCredential) and gets their own
    TradingLoop/rate-limit budget -- see runtime/loop_registry.py.
    password_hash is set at signup (auth/security.py); there is no email
    verification or self-serve password reset in v1 -- see auth/routes.py's
    docstring for why."""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class BrokerCredential(Base):
    """A user's own Webull API key/secret, encrypted at rest (see
    auth/crypto.py) -- this is what gives each user their own independent
    Webull rate-limit budget instead of sharing the operator's. `broker`
    exists to leave room for a non-Webull broker later without a schema
    change, though only "webull" is used today. `live_trading_enabled` is
    the per-user self-serve live-trading toggle (the dashboard's Connect
    Broker Account panel) -- Settings.is_live_trading_authorized()'s
    process-wide env-var gate still applies underneath as a second,
    deployment-level safety layer; this column is the per-user layer on
    top of it, not a replacement for it."""
    __tablename__ = "broker_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, index=True)
    broker: Mapped[str] = mapped_column(String(32), default="webull")
    app_key_encrypted: Mapped[str] = mapped_column(Text)
    app_secret_encrypted: Mapped[str] = mapped_column(Text)
    account_id_encrypted: Mapped[str] = mapped_column(Text)
    base_url: Mapped[str] = mapped_column(String(255))
    trading_mode: Mapped[str] = mapped_column(String(16), default="sandbox")
    live_trading_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_verify_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Bot(Base):
    """One entry in a user's multi-bot dashboard (2026-08-15 multi-bot
    framework) -- e.g. "Day Trading Quant", the existing momentum system,
    registered as every user's first bot. All of a user's bots share that
    same user's single BrokerCredential/capital (no per-bot broker
    connection) -- what's independent per bot is trading history/P&L
    only, via the bot_id column on TradeRecord/OrderRecord/ScannerEvent/
    MomentumScoreRecord/MomentumEventRecord below. `kind` identifies which
    engine backs this bot ("momentum_quant" today); `slug` is a stable,
    URL/lookup-safe identifier distinct from the user-facing `name`."""
    __tablename__ = "bots"
    __table_args__ = (UniqueConstraint("user_id", "slug", name="uq_bots_user_slug"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    slug: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(32))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MomentumEventRecord(Base):
    """
    Traded AND non-traded momentum events, with forward-looking outcome
    windows. This is the core data-collection artifact for improving the
    MIS formula and entry strategies offline.
    """
    __tablename__ = "momentum_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # See ScannerEvent.user_id's comment -- same nullable-for-backfill
    # rationale.
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    bot_id: Mapped[int | None] = mapped_column(ForeignKey("bots.id"), nullable=True, index=True)
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

    # -- Real-Time Momentum Qualification Layer (2026-08-17) ------------------
    # See models.MomentumEvent's own docstring on these four fields --
    # additive nullable columns, picked up by db/session.py's sync_schema()
    # on next startup with no Alembic migration needed, same as every other
    # column in this table.
    outcome_5s: Mapped[dict | None] = mapped_column(_json_type(), nullable=True)
    outcome_10s: Mapped[dict | None] = mapped_column(_json_type(), nullable=True)
    outcome_15s: Mapped[dict | None] = mapped_column(_json_type(), nullable=True)
    momentum_qualification_at_event: Mapped[dict | None] = mapped_column(_json_type(), nullable=True)
