from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings, get_settings
from .models import Base

logger = logging.getLogger(__name__)

_engine = None
_SessionLocal = None


def get_engine(settings: Settings | None = None):
    global _engine
    if _engine is None:
        settings = settings or get_settings()
        # Real incident (2026-08-26): the dashboard intermittently 500'd on
        # /api/performance, /api/candidates, /api/status, /api/trades, and
        # /api/score-breakdown -- data would load, then one poll cycle would
        # 500, then it would come back. Every one of those endpoints opens a
        # session here on every request (auth/dependencies.py's
        # get_current_user checks the logged-in user even on routes that
        # don't otherwise touch the DB), so a single pooled connection that
        # the database (or an intermediary connection pooler, e.g. a managed
        # Postgres/Supabase setup's PgBouncer -- see README.md) silently
        # closed while idle would fail on its next checkout with something
        # like "server closed the connection unexpectedly", then get
        # discarded, then the very next request would get a fresh connection
        # and succeed -- exactly the fail-once-then-recover pattern
        # reported, surfacing on whichever endpoint happened to draw the
        # dead connection. pool_pre_ping issues a cheap "SELECT 1" before
        # handing out a pooled connection and transparently reconnects if
        # it's dead, so this is never visible to a caller; pool_recycle is a
        # second line of defense that proactively retires connections before
        # a pooler's own idle-close window can hit them.
        _engine = create_engine(settings.database.url, future=True, pool_pre_ping=True, pool_recycle=1800)
    return _engine


def get_session_factory(settings: Settings | None = None) -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(settings), expire_on_commit=False, future=True)
    return _SessionLocal


@contextmanager
def session_scope(settings: Settings | None = None) -> Iterator[Session]:
    session = get_session_factory(settings)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def sync_schema(settings: Settings | None = None) -> None:
    """Lightweight stand-in for a real migration tool (see db/models.py's
    module docstring: "No Alembic migration is included yet"). create_all()
    below only creates tables that don't exist AT ALL -- it silently does
    nothing to a table that already exists, even if the model has since
    grown new columns. On a short-lived test SQLite DB (fresh every run)
    that's invisible, but on the VPS's long-lived Postgres DB it's a real
    trap: add a column to any model here, and every INSERT against that
    table starts failing from that point on, forever, with no schema error
    surfaced anywhere except whatever `except Exception: logger.exception(...)`
    happens to wrap the call site (see e.g. scripts/run_dashboard.py's
    on_trade_closed) -- which reads as "nothing happened" to anyone not
    actively watching that log.

    Real incident this fixes (2026-08-11): the dashboard's Performance/
    Trade History cards stayed permanently empty despite trades actually
    closing on the live bot, because the VPS's `trades` table predated
    several TradeRecord columns (max_favorable_excursion,
    max_adverse_excursion, trading_mode -- added well after that table was
    first created) -- every record_trade() call had been failing and
    getting silently swallowed the entire time.

    What this does: for every table create_all() finds already existing,
    compares its actual live columns (via SQLAlchemy's inspector) against
    what the model expects, and ALTERs in whatever's missing -- then, as
    of the multi-bot framework (2026-08-15), also checks every column the
    model declares `index=True` on and CREATE INDEXes any that's missing,
    whether that column was just added above or was added by an earlier
    run of this same function before this index check existed (the real
    incident this closes: bot_id was added this way to orders/trades/
    scanner_events/momentum_scores/momentum_events with no index, and an
    unindexed filter against momentum_scores -- written on every tick for
    every watched candidate, so it grows huge -- is a full table scan;
    that's what turned into the /api/score-breakdown 504 this fixes).
    Deliberately narrow, not a real migration tool:
      - Additive only. Never drops/renames/retypes a column or index,
        never adds a constraint (NOT NULL, a default, a foreign key) --
        every added column is bare and nullable, so this can never fail
        against existing rows (they just get NULL for it) and can never
        be destructive.
      - No data backfill. A newly-added column starts NULL for every
        existing row; callers that need real values there for old rows
        need their own one-off backfill, this won't attempt one.
      - Reach for a real migration tool (Alembic) the moment schema
        evolution needs anything this doesn't cover -- this exists to
        close the specific "silent write failure"/"silent full table
        scan" gaps above cheaply, not to be a permanent substitute."""
    engine = get_engine(settings)
    inspector = inspect(engine)
    preparer = engine.dialect.identifier_preparer
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue  # brand new -- create_all() above already made it fully correct
            existing_columns = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing_columns:
                    continue
                column_type = column.type.compile(dialect=engine.dialect)
                conn.execute(text(
                    f"ALTER TABLE {preparer.quote(table.name)} "
                    f"ADD COLUMN {preparer.quote(column.name)} {column_type}"
                ))
                existing_columns.add(column.name)
                logger.warning(
                    "sync_schema: %s.%s existed without column %r (schema drift against an "
                    "already-created table -- see this function's docstring) -- added it, "
                    "nullable, NULL for every existing row.",
                    table.name, column.name, column.name,
                )

            # Single-column indexes only -- every index=True column on
            # this project's models is a single-column index (see
            # db/models.py); a composite index would need its own
            # explicit handling this doesn't attempt.
            existing_indexed_columns = {
                idx["column_names"][0]
                for idx in inspector.get_indexes(table.name)
                if len(idx["column_names"]) == 1
            }
            for column in table.columns:
                if not column.index or column.name in existing_indexed_columns:
                    continue
                index_name = f"ix_{table.name}_{column.name}"
                conn.execute(text(
                    f"CREATE INDEX {preparer.quote(index_name)} "
                    f"ON {preparer.quote(table.name)} ({preparer.quote(column.name)})"
                ))
                logger.warning(
                    "sync_schema: %s.%s had no index despite the model declaring index=True "
                    "(schema drift -- see this function's docstring) -- created %r.",
                    table.name, column.name, index_name,
                )


def create_all(settings: Settings | None = None) -> None:
    Base.metadata.create_all(get_engine(settings))
    sync_schema(settings)
