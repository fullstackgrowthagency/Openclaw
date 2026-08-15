"""cutover: seed user 1, backfill user_id, enforce NOT NULL

Production cutover for the existing single-tenant deployment (2026-08-15
multi-tenant conversion) -- see docs/ARCHITECTURE.md's "Multi-tenant
auth" section for the full design. This is the one genuinely structural
migration db/session.py's sync_schema() cannot do on its own (NOT NULL
constraints, backfill, real foreign keys) -- see 0001_baseline.py's
docstring for why every other schema change in this conversion (the new
users/broker_credentials tables, the new nullable user_id columns) went
through sync_schema() automatically instead, with no migration needed.

Requires two things already set in the environment before running:
  - CUTOVER_OPERATOR_EMAIL: the email address that becomes "user 1",
    taking ownership of every row already in the database. Read directly
    from the environment (not hardcoded) so this file carries no real
    account information.
  - WEBULL_APP_KEY/WEBULL_APP_SECRET/WEBULL_ACCOUNT_ID/WEBULL_BASE_URL/
    TRADING_MODE/CREDENTIAL_ENCRYPTION_KEY -- this deployment's existing
    Webull credentials (the ones already running in production) and the
    key to encrypt them with. Seeds user 1's broker_credentials row so
    the already-connected account keeps working with zero re-entry.

Marks that seeded credential row pre-verified (last_verified_at=now):
this exact key/secret/account_id/base_url has already been running live
against this exact deployment's whole history -- there is nothing a
fresh verification call would prove that today's already-running process
hasn't already proven, and LoopRegistry's startup sweep
(scripts/run_dashboard.py) only starts a loop for a verified account, so
this is what makes user 1's loop come back up automatically on restart.

Does NOT set a real password -- password_hash starts as an unusable
placeholder. Run scripts/set_operator_password.py (a separate, manual,
interactive step) before anyone should be able to log in as user 1;
never embed a real password in a migration file.

Backfills every existing row in the tables db/repository.py actually
writes to (orders/trades/scanner_events/momentum_scores/momentum_events
-- see 0001_baseline.py's sibling docstring for why positions/fills/
risk_events/signals/backtest_results/performance_stats are untouched)
to the seeded user, THEN makes user_id NOT NULL with a real foreign key
-- safe specifically because the backfill above guarantees no row is
left NULL by the time that constraint is added.

SQLite (this project's local dev/test DB only, never production
Postgres) can't ALTER COLUMN to add a NOT NULL constraint or a
first-class foreign key without a full table rebuild -- this migration
skips that part of the upgrade on sqlite (backfill still runs) so local
runs/tests can still exercise the rest of it; the real target
(Postgres) gets the full enforcement.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-15
"""
from __future__ import annotations

import os
from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_SCOPED_TABLES = ["orders", "trades", "scanner_events", "momentum_scores", "momentum_events"]
_PLACEHOLDER_PASSWORD_HASH = "!disabled-run-scripts-set_operator_password.py-before-login"


def upgrade() -> None:
    from webull_bot.auth.crypto import encrypt_secret
    from webull_bot.config import get_settings
    from webull_bot.db.models import Base

    settings = get_settings()
    operator_email = os.environ.get("CUTOVER_OPERATOR_EMAIL", "").strip().lower()
    if not operator_email or "@" not in operator_email:
        raise RuntimeError(
            "CUTOVER_OPERATOR_EMAIL must be set to a real email address before running this "
            "migration -- it becomes 'user 1', owning every row already in this database."
        )
    if not settings.webull.is_configured():
        raise RuntimeError(
            "WEBULL_APP_KEY/WEBULL_APP_SECRET/WEBULL_ACCOUNT_ID must still be set in the "
            "environment -- this migration seeds user 1's broker_credentials row from them."
        )
    if not settings.credential_encryption_key:
        raise RuntimeError("CREDENTIAL_ENCRYPTION_KEY must be set before running this migration.")

    conn = op.get_bind()
    now = datetime.utcnow()
    is_sqlite = conn.dialect.name == "sqlite"

    # Reflects the real, fully-defined tables (with primary-key metadata
    # SQLAlchemy needs to report inserted_primary_key below) straight off
    # db/models.py's own Base.metadata, rather than a bare ad-hoc
    # sa.table() -- both users and broker_credentials already exist by
    # the time this migration runs (created additively by sync_schema(),
    # see 0001_baseline.py's docstring), so this is describing, not
    # creating, the schema.
    users = Base.metadata.tables["users"]
    broker_credentials = Base.metadata.tables["broker_credentials"]

    result = conn.execute(
        users.insert().values(
            email=operator_email, password_hash=_PLACEHOLDER_PASSWORD_HASH, is_active=True, created_at=now,
        )
    )
    user_id = result.inserted_primary_key[0]

    conn.execute(
        broker_credentials.insert().values(
            user_id=user_id,
            broker="webull",
            app_key_encrypted=encrypt_secret(settings.webull.app_key, settings),
            app_secret_encrypted=encrypt_secret(settings.webull.app_secret, settings),
            account_id_encrypted=encrypt_secret(settings.webull.account_id, settings),
            base_url=settings.webull.base_url,
            trading_mode=settings.trading_mode.value,
            live_trading_enabled=False,
            last_verified_at=now,
            created_at=now,
            updated_at=now,
        )
    )

    for table in _SCOPED_TABLES:
        conn.execute(sa.text(f"UPDATE {table} SET user_id = :user_id WHERE user_id IS NULL"), {"user_id": user_id})
        if is_sqlite:
            continue
        conn.execute(sa.text(f"ALTER TABLE {table} ALTER COLUMN user_id SET NOT NULL"))
        op.create_foreign_key(f"fk_{table}_user_id", table, "users", ["user_id"], ["id"])


def downgrade() -> None:
    """Relaxes the constraints added above; deliberately does NOT delete
    the seeded user/broker_credentials row or un-backfill user_id (both
    would be destructive against real production data by the time anyone
    would realistically run this downgrade) -- this only undoes the
    schema-level enforcement, matching this migration's own narrow
    "structural change only" scope."""
    conn = op.get_bind()
    if conn.dialect.name == "sqlite":
        return
    for table in _SCOPED_TABLES:
        op.drop_constraint(f"fk_{table}_user_id", table, type_="foreignkey")
        conn.execute(sa.text(f"ALTER TABLE {table} ALTER COLUMN user_id DROP NOT NULL"))
