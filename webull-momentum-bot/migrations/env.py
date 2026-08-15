"""Alembic environment. Reads DATABASE_URL from webull_bot.config.Settings
(the same source of truth db/session.py and run_dashboard.py already use)
rather than a separate env parse, and targets db/models.py's Base.metadata
directly -- see db/models.py's module docstring for why a real migration
tool was introduced (sync_schema() can't do structural changes like the
NOT NULL foreign keys the multi-tenant conversion needs).

Migration history starts at revision 0001, a no-op "baseline" stamped
(not upgraded) against any database that already has the hand-managed
schema from before Alembic existed -- see versions/0001_baseline.py."""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from webull_bot.config import get_settings
from webull_bot.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database.url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url, target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"}
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
