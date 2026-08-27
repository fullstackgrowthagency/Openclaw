from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event, inspect, text
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
        # 500, then it would come back. pool_pre_ping/pool_recycle below were
        # a first attempt at this, reasoning from this project's coded
        # DATABASE_URL default (Postgres) that a silently-dropped pooled
        # connection was the cause -- harmless, but wrong for THIS
        # deployment: the VPS's actual DATABASE_URL points at a local
        # SQLite file (confirmed via a real traceback:
        # `sqlite3.OperationalError: database is locked`), which pool
        # settings do nothing for. The real mechanism, specific to SQLite:
        # this one process's background thread commits a write (a momentum
        # score/scanner event, persisted on every tick via
        # scripts/run_dashboard.py's on_score_computed/on_state_transition)
        # at the same moment a dashboard request opens its own session
        # (every /api/* route does, via auth/dependencies.py's
        # get_current_user) -- SQLite's default rollback-journal mode
        # blocks a reader for the duration of a writer's commit and fails
        # fast rather than waiting, which is exactly the
        # fail-once-then-recover pattern reported. See the sqlite-specific
        # PRAGMAs registered below for the actual fix.
        _engine = create_engine(settings.database.url, future=True, pool_pre_ping=True, pool_recycle=1800)
        if _engine.dialect.name == "sqlite":
            # WAL (write-ahead log) journal mode lets readers proceed
            # without blocking on a concurrent writer's commit (the
            # opposite of the default rollback-journal mode's behavior
            # that caused the incident above) -- readers and the one
            # active writer no longer contend for the same lock. Writers
            # still serialize against each other (SQLite only ever allows
            # one writer at a time, WAL or not), so busy_timeout is a
            # second line of defense: any connection that still can't get
            # the lock it needs waits and retries for up to 30s instead of
            # raising "database is locked" immediately. Applied via a
            # `connect` event (not a `connect_args={"timeout": ...}`
            # kwarg) so it also covers journal_mode, which has no
            # equivalent sqlite3.connect() parameter.
            #
            # Real incident (2026-08-27, same day): WAL + busy_timeout
            # alone didn't stop the lock errors -- 300 "database is
            # locked" plus 168 connection-pool-timeout errors in a single
            # hour, still dwarfing every other exception type in
            # journalctl. Root cause: this process persists a momentum
            # score or scanner event with its own commit for every tracked
            # candidate on every 5s tick (scripts/run_dashboard.py's
            # on_score_computed/on_state_transition, one commit per call,
            # no batching) -- with 100+ tracked candidates that's dozens
            # of commits every 5 seconds, and SQLite's default
            # synchronous=FULL fsyncs the WAL on every single one of them.
            # Under real disk contention that fsync cost directly extends
            # how long each commit holds SQLite's one writer lock, which
            # is what turns "occasional contention" into hundreds of
            # failures an hour. synchronous=NORMAL is SQLite's own
            # documented companion setting for WAL mode: safe (durable
            # against this process crashing; only risks losing the most
            # recent commit(s) on an actual OS crash/power loss, an
            # acceptable tradeoff for scanner-event/momentum-score
            # telemetry) and skips that fsync, syncing only at WAL
            # checkpoints instead of every commit.
            @event.listens_for(_engine, "connect")
            def _set_sqlite_pragmas(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.close()
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
