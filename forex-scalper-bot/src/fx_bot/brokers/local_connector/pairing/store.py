"""
PairingStore -- sqlite3-backed persistence for pairing codes and
connector bearer tokens. Bare stdlib sqlite3, not SQLAlchemy: two tiny
tables, low write volume -- introducing an ORM/migration story now would
mean guessing at Phase 10's real multi-tenant schema under no pressure
to do so yet; migrating these two tables into a real schema later is a
trivial, well-scoped follow-up.

Called from two different threads in practice: FastAPI's request
threadpool (the two HTTP routes) and RelayServer's shared event-loop
thread (via run_in_executor, during the auth handshake) -- sqlite3
connections are not safe for concurrent use across threads even with
check_same_thread=False, so every operation here is guarded by a single
`threading.Lock`.

Both tables carry an `account_id` column from day one -- a single fixed
value today (see pairing/routes.py's DEFAULT_ACCOUNT_ID), mirroring how
the wire envelope (relay_protocol.envelope.Envelope.make_auth) already
carries account_id ahead of Phase 10's real multi-tenancy, so that
phase extends these two tables rather than reshaping them.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Union

from .codes import generate_pairing_code
from .exceptions import PairingCodeAlreadyUsed, PairingCodeExpired, PairingCodeNotFound

_ISO = "%Y-%m-%dT%H:%M:%S.%f"


def _now_str() -> str:
    return datetime.utcnow().strftime(_ISO)


def _parse(value: str) -> datetime:
    return datetime.strptime(value, _ISO)


@dataclass
class IssuedPairingCode:
    code: str
    account_id: str
    expires_at: datetime


class PairingStore:
    def __init__(self, db_path: Union[str, Path]):
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pairing_codes (
                    code TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS connector_tokens (
                    token_hash TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    revoked_at TEXT
                )
                """
            )

    def create_pairing_code(self, account_id: str, *, ttl_seconds: float) -> IssuedPairingCode:
        now = datetime.utcnow()
        expires_at = now + timedelta(seconds=ttl_seconds)
        code = generate_pairing_code()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO pairing_codes (code, account_id, created_at, expires_at, used_at) "
                "VALUES (?, ?, ?, ?, NULL)",
                (code, account_id, now.strftime(_ISO), expires_at.strftime(_ISO)),
            )
        return IssuedPairingCode(code=code, account_id=account_id, expires_at=expires_at)

    def consume_pairing_code(self, code: str) -> str:
        """Single locked SELECT-then-UPDATE-used_at operation, so two
        concurrent /connector/pair calls for the same code can't both
        succeed. Raises PairingCodeNotFound/PairingCodeExpired/
        PairingCodeAlreadyUsed -- never returns a falsy/None sentinel for
        a failure a caller might silently ignore."""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT account_id, expires_at, used_at FROM pairing_codes WHERE code = ?", (code,),
            ).fetchone()
            if row is None:
                raise PairingCodeNotFound(f"No pairing code {code!r} was ever issued.")
            account_id, expires_at_str, used_at_str = row
            if used_at_str is not None:
                raise PairingCodeAlreadyUsed(f"Pairing code {code!r} has already been used.")
            if _parse(expires_at_str) < datetime.utcnow():
                raise PairingCodeExpired(f"Pairing code {code!r} has expired.")
            self._conn.execute("UPDATE pairing_codes SET used_at = ? WHERE code = ?", (_now_str(), code))
        return account_id

    def store_token(self, account_id: str, token_hash: str) -> None:
        """Revokes every existing non-revoked token for `account_id`
        first -- v1 has exactly one account_id, so this is a full
        rotation on every re-pair ("the old one invalidated" rule from
        the approved design)."""
        now = _now_str()
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE connector_tokens SET revoked_at = ? WHERE account_id = ? AND revoked_at IS NULL",
                (now, account_id),
            )
            self._conn.execute(
                "INSERT INTO connector_tokens (token_hash, account_id, created_at, revoked_at) "
                "VALUES (?, ?, ?, NULL)",
                (token_hash, account_id, now),
            )

    def lookup_account_id_by_token_hash(self, token_hash: str) -> Optional[str]:
        """The read path the relay-connection auth handshake calls on
        every new socket. Returns None for no match or a revoked token --
        never raises, since "not a valid token" is an expected outcome
        here, not an error condition."""
        with self._lock:
            row = self._conn.execute(
                "SELECT account_id FROM connector_tokens WHERE token_hash = ? AND revoked_at IS NULL",
                (token_hash,),
            ).fetchone()
        return row[0] if row is not None else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()
