"""seed default bots: one "Day Trading Quant" bot per existing user, backfill bot_id

Multi-bot framework (2026-08-15) -- see docs/ARCHITECTURE.md and
db/models.py's Bot class. The `bots` table itself and the new nullable
`bot_id` columns on orders/trades/scanner_events/momentum_scores/
momentum_events need no migration to come into existence (sync_schema()
adds them additively, same as user_id originally -- see
0002_cutover_backfill_user_id.py's docstring for why that's true and
when a real migration IS required). What sync_schema() cannot do is
what this migration handles: give every user who already exists a
default bot, and backfill bot_id onto their existing rows in those five
tables, exactly mirroring 0002's user_id backfill one dimension deeper.

Unlike 0002 (which seeded exactly one hardcoded operator account),
this runs for however many users already exist -- so the backfill is
done per-user in Python rather than a single constant UPDATE, avoiding
any dialect-specific UPDATE-FROM/join syntax differences between
SQLite (this project's local dev/test DB) and Postgres (production).

Same is_sqlite guard as 0002 for the NOT NULL + FK enforcement step:
SQLite can't ALTER COLUMN to add either without a full table rebuild,
so that enforcement is skipped there (the backfill itself still runs).

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-15
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_SCOPED_TABLES = ["orders", "trades", "scanner_events", "momentum_scores", "momentum_events"]
_DEFAULT_BOT_SLUG = "day-trading-quant"
_DEFAULT_BOT_NAME = "Day Trading Quant"
_DEFAULT_BOT_KIND = "momentum_quant"


def upgrade() -> None:
    from webull_bot.db.models import Base

    conn = op.get_bind()
    is_sqlite = conn.dialect.name == "sqlite"

    users = Base.metadata.tables["users"]
    bots = Base.metadata.tables["bots"]

    existing_user_ids = [row[0] for row in conn.execute(sa.select(users.c.id))]

    for user_id in existing_user_ids:
        existing_bot = conn.execute(
            sa.select(bots.c.id).where(bots.c.user_id == user_id, bots.c.slug == _DEFAULT_BOT_SLUG)
        ).first()
        if existing_bot is not None:
            bot_id = existing_bot[0]
        else:
            result = conn.execute(
                bots.insert().values(
                    user_id=user_id, slug=_DEFAULT_BOT_SLUG, name=_DEFAULT_BOT_NAME,
                    kind=_DEFAULT_BOT_KIND, is_active=True,
                )
            )
            bot_id = result.inserted_primary_key[0]

        for table in _SCOPED_TABLES:
            conn.execute(
                sa.text(f"UPDATE {table} SET bot_id = :bot_id WHERE user_id = :user_id AND bot_id IS NULL"),
                {"bot_id": bot_id, "user_id": user_id},
            )

    if is_sqlite:
        return
    for table in _SCOPED_TABLES:
        conn.execute(sa.text(f"ALTER TABLE {table} ALTER COLUMN bot_id SET NOT NULL"))
        op.create_foreign_key(f"fk_{table}_bot_id", table, "bots", ["bot_id"], ["id"])


def downgrade() -> None:
    """Relaxes the constraints added above; deliberately does NOT delete
    the seeded bot rows or un-backfill bot_id (both would be destructive
    against real production data by the time anyone would realistically
    run this downgrade) -- matches 0002's downgrade philosophy exactly."""
    conn = op.get_bind()
    if conn.dialect.name == "sqlite":
        return
    for table in _SCOPED_TABLES:
        op.drop_constraint(f"fk_{table}_bot_id", table, type_="foreignkey")
        conn.execute(sa.text(f"ALTER TABLE {table} ALTER COLUMN bot_id DROP NOT NULL"))
