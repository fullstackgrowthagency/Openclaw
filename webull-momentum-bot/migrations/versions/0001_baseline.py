"""baseline

Marks the existing hand-managed schema (created via db/session.py's
create_all()/sync_schema(), see db/models.py's module docstring) as
Alembic's starting point. Deliberately a no-op: those tables already
exist on any real deployment (including the VPS), so this migration must
never try to (re)create them. It exists so `alembic stamp head` has a
revision to stamp an existing database at, once a real migration
(genuine ALTERs -- NOT NULL constraints, backfills, renames -- the kind
of change db/session.py's sync_schema() docstring explicitly says it
cannot do) is actually needed.

2026-08-15 multi-tenant conversion note: adding the `users`/
`broker_credentials` tables and the new nullable `user_id` columns on
orders/trades/scanner_events/momentum_scores/momentum_events did NOT get
their own Alembic migrations, on purpose -- every one of those changes is
purely additive (new tables, or new NULLABLE columns on existing ones),
which is exactly what create_all()/sync_schema() already do automatically
on every process startup (see db/session.py's create_all()). Two
mechanisms doing the same additive work is redundant, not safer -- worse,
running both against the same database can actively conflict (confirmed
while building this: create_all() creates a brand-new table, then a
migration trying to CREATE TABLE it again fails outright). Alembic
remains the right tool for the one change sync_schema() genuinely can't
do: eventually making user_id NOT NULL with a real backfill of existing
rows to the operator's own account, as part of the production cutover
(see docs/ARCHITECTURE.md's "Multi-tenant auth" section) -- that
migration gets written when that cutover actually happens, not
speculatively now.

Revision ID: 0001
Revises:
Create Date: 2026-08-15
"""
from __future__ import annotations

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
