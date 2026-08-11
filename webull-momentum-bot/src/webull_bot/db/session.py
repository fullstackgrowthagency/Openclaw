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
        _engine = create_engine(settings.database.url, future=True)
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
    what the model expects, and ALTERs in whatever's missing. Deliberately
    narrow, not a real migration tool:
      - Additive only. Never drops/renames/retypes a column, never adds a
        constraint (NOT NULL, a default, a foreign key) -- every added
        column is bare and nullable, so this can never fail against
        existing rows (they just get NULL for it) and can never be
        destructive.
      - No data backfill. A newly-added column starts NULL for every
        existing row; callers that need real values there for old rows
        need their own one-off backfill, this won't attempt one.
      - Reach for a real migration tool (Alembic) the moment schema
        evolution needs anything this doesn't cover -- this exists to
        close the specific "silent write failure" gap above cheaply, not
        to be a permanent substitute."""
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
                logger.warning(
                    "sync_schema: %s.%s existed without column %r (schema drift against an "
                    "already-created table -- see this function's docstring) -- added it, "
                    "nullable, NULL for every existing row.",
                    table.name, column.name, column.name,
                )


def create_all(settings: Settings | None = None) -> None:
    Base.metadata.create_all(get_engine(settings))
    sync_schema(settings)
