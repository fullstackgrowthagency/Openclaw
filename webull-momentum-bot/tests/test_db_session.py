"""
Tests for db/session.py's sync_schema/create_all -- the lightweight
stand-in for a real migration tool (see db/models.py's module docstring).

The real incident this covers (2026-08-11): the VPS's `trades` table
predated several TradeRecord columns (max_favorable_excursion,
max_adverse_excursion, trading_mode), so every record_trade() call had
been silently failing since those columns were added -- Base.metadata.
create_all() only creates brand-new tables, it never alters one that
already exists. sync_schema() closes that gap by diffing each existing
table's live columns against the model and ALTERing in whatever's
missing.
"""
from datetime import datetime

import webull_bot.db.session as db_session
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from webull_bot.db.models import Base, TradeRecord
from webull_bot.enums import ExitReason, OrderSide


@pytest.fixture(autouse=True)
def _reset_engine_singleton(monkeypatch):
    # db/session.py caches _engine/_SessionLocal at module scope (get_engine
    # only builds one the first time it's called) -- reset both before and
    # after every test so one test's engine can never leak into another's.
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_SessionLocal", None)
    yield
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_SessionLocal", None)


def _sqlite_engine():
    # Plain sqlite:///:memory: (not sqlite://), same as test_repository.py's
    # `session` fixture -- SQLAlchemy keeps one connection alive per thread
    # for a memory URL by default, which is enough for a single-threaded
    # test that reuses this exact engine object throughout.
    return create_engine("sqlite:///:memory:")


def _create_pre_2026_08_11_trades_table(engine):
    """A trades table shaped like it would have been before
    max_favorable_excursion/max_adverse_excursion/trading_mode existed on
    TradeRecord -- the real drift this project hit."""
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE trades ("
            "id INTEGER PRIMARY KEY, symbol VARCHAR(16), strategy_name VARCHAR(64), "
            "side VARCHAR(16), entry_price FLOAT, exit_price FLOAT, quantity FLOAT, "
            "opened_at DATETIME, closed_at DATETIME, exit_reason VARCHAR(32), "
            "pnl FLOAT, pnl_pct FLOAT"
            ")"
        ))


def test_sync_schema_adds_missing_columns_to_an_existing_table(monkeypatch):
    engine = _sqlite_engine()
    _create_pre_2026_08_11_trades_table(engine)
    monkeypatch.setattr(db_session, "_engine", engine)

    db_session.sync_schema()

    columns = {c["name"] for c in inspect(engine).get_columns("trades")}
    assert "max_favorable_excursion" in columns
    assert "max_adverse_excursion" in columns
    assert "trading_mode" in columns


def test_sync_schema_lets_a_previously_failing_insert_succeed(monkeypatch):
    # The actual end-to-end proof: a record_trade()-shaped insert that
    # would have failed against the drifted table now succeeds after
    # sync_schema, and the previously-missing columns round-trip correctly.
    engine = _sqlite_engine()
    _create_pre_2026_08_11_trades_table(engine)
    monkeypatch.setattr(db_session, "_engine", engine)
    db_session.sync_schema()

    with Session(engine) as session:
        session.add(TradeRecord(
            symbol="GME", strategy_name="momentum_breakout", side=OrderSide.BUY.value,
            entry_price=5.0, exit_price=5.5, quantity=100,
            opened_at=datetime(2026, 8, 11, 15, 0, 0), closed_at=datetime(2026, 8, 11, 15, 10, 0),
            exit_reason=ExitReason.PROFIT_TARGET.value,
            pnl=50.0, pnl_pct=10.0, max_favorable_excursion=12.0, max_adverse_excursion=1.0,
            trading_mode="paper",
        ))
        session.commit()

    with Session(engine) as session:
        row = session.query(TradeRecord).one()
        assert row.max_favorable_excursion == 12.0
        assert row.trading_mode == "paper"


def test_sync_schema_backfills_null_for_existing_rows_missing_column(monkeypatch):
    engine = _sqlite_engine()
    _create_pre_2026_08_11_trades_table(engine)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO trades (symbol, strategy_name, side, entry_price, exit_price, "
            "quantity, exit_reason, pnl, pnl_pct) VALUES "
            "('GME', 'test', 'buy', 5.0, 5.5, 100, 'profit_target', 50.0, 10.0)"
        ))
    monkeypatch.setattr(db_session, "_engine", engine)

    db_session.sync_schema()

    with Session(engine) as session:
        row = session.query(TradeRecord).one()
        assert row.max_favorable_excursion is None
        assert row.trading_mode is None


def test_sync_schema_is_a_noop_when_nothing_has_drifted(monkeypatch):
    engine = _sqlite_engine()
    Base.metadata.create_all(engine)  # a fully up-to-date schema from the start
    monkeypatch.setattr(db_session, "_engine", engine)

    db_session.sync_schema()  # must not raise, must not duplicate/alter anything

    columns_after = {c["name"] for c in inspect(engine).get_columns("trades")}
    expected = {c.name for c in TradeRecord.__table__.columns}
    assert columns_after == expected


def test_sync_schema_skips_a_table_that_does_not_exist_at_all(monkeypatch):
    # No tables created at all -- nothing for sync_schema to find via
    # inspector.has_table, so this must be a clean no-op (create_all()
    # is what's responsible for brand-new tables, not this function).
    engine = _sqlite_engine()
    monkeypatch.setattr(db_session, "_engine", engine)

    db_session.sync_schema()

    assert inspect(engine).get_table_names() == []


def test_sync_schema_creates_missing_index_for_an_existing_column(monkeypatch):
    # The real incident this covers (2026-08-15): bot_id got added to
    # orders/trades/scanner_events/momentum_scores/momentum_events via
    # sync_schema's ADD COLUMN, but nothing ever created its index --
    # an unindexed filter against the huge momentum_scores table (written
    # on every tick for every watched candidate) is a full table scan,
    # which is what turned into a 504 on /api/score-breakdown.
    engine = _sqlite_engine()
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE trades ("
            "id INTEGER PRIMARY KEY, symbol VARCHAR(16), strategy_name VARCHAR(64), "
            "side VARCHAR(16), entry_price FLOAT, exit_price FLOAT, quantity FLOAT, "
            "opened_at DATETIME, closed_at DATETIME, exit_reason VARCHAR(32), "
            "pnl FLOAT, pnl_pct FLOAT, max_favorable_excursion FLOAT, "
            "max_adverse_excursion FLOAT, trading_mode VARCHAR(16), "
            "user_id INTEGER, bot_id INTEGER"
            ")"
        ))
    monkeypatch.setattr(db_session, "_engine", engine)

    db_session.sync_schema()

    indexed_columns = {
        idx["column_names"][0]
        for idx in inspect(engine).get_indexes("trades")
        if len(idx["column_names"]) == 1
    }
    assert "bot_id" in indexed_columns
    assert "user_id" in indexed_columns
    assert "symbol" in indexed_columns


def test_sync_schema_index_check_is_a_noop_when_indexes_already_exist(monkeypatch):
    engine = _sqlite_engine()
    Base.metadata.create_all(engine)  # a fully up-to-date schema, indexes included
    monkeypatch.setattr(db_session, "_engine", engine)

    indexes_before = inspect(engine).get_indexes("trades")
    db_session.sync_schema()  # must not raise, must not duplicate any index
    indexes_after = inspect(engine).get_indexes("trades")

    assert len(indexes_after) == len(indexes_before)


def test_create_all_both_creates_new_tables_and_fixes_a_drifted_one(monkeypatch):
    engine = _sqlite_engine()
    _create_pre_2026_08_11_trades_table(engine)  # trades exists but is drifted
    monkeypatch.setattr(db_session, "_engine", engine)

    db_session.create_all()

    inspector = inspect(engine)
    # Every other mapped table got created fresh...
    assert "risk_events" in inspector.get_table_names()
    # ...and the pre-existing, drifted trades table got its missing
    # columns added rather than being left alone.
    trades_columns = {c["name"] for c in inspector.get_columns("trades")}
    assert "trading_mode" in trades_columns
