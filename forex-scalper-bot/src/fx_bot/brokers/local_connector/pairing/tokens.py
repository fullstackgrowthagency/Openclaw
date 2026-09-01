"""
Connector bearer tokens. Hashed with plain SHA-256, deliberately NOT
webull_bot's Fernet (reversible encryption, for secrets that must be
read back in plaintext -- a bearer token never is: every future use is
"does an inbound token match a stored hash") or bcrypt (slow, salted
hashing meant to resist brute-forcing low-entropy human-guessable
passwords -- a 256-bit token already has entropy no realistic attack
touches, so bcrypt's deliberate slowness would only add latency to every
relay-connection handshake for a threat model that doesn't apply here).
"""
from __future__ import annotations

import hashlib
import secrets
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from .store import PairingStore


def generate_token() -> str:
    return secrets.token_urlsafe(32)  # 256 bits of entropy


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def make_authenticator(store: "PairingStore") -> Callable[[str], Optional[str]]:
    """Builds the plain sync `token -> account_id | None` callable
    RelayServer takes -- keeps relay_server.py/relay_connection.py
    decoupled from PairingStore and the hashing scheme entirely, the
    same reasoning RelayConnection's own `_Transport` Protocol already
    applies to the socket layer."""
    return lambda token: store.lookup_account_id_by_token_hash(hash_token(token))
