"""Password hashing for User.password_hash. Calls the `bcrypt` library
directly rather than going through passlib's CryptContext: passlib 1.7.4
(its last release) is unmaintained and its bcrypt backend crashes outright
against bcrypt>=4.1 (removed the `__about__` attribute passlib's version
probe reads) -- confirmed while wiring this up. bcrypt itself enforces a
72-byte input cap; passwords are truncated to that (matching bcrypt's own
documented behavior) rather than raising, since a 72+ byte password isn't
a real usability concern for this app."""
from __future__ import annotations

import bcrypt

_BCRYPT_MAX_BYTES = 72


def _truncate(password: str) -> bytes:
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_truncate(password), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_truncate(password), password_hash.encode("ascii"))
    except ValueError:
        # Malformed/foreign hash format -- treat as a failed verification,
        # not a crash.
        return False
