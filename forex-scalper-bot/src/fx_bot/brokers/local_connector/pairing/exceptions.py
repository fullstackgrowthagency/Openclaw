"""
Exceptions raised by PairingStore.consume_pairing_code -- see store.py.
"""
from __future__ import annotations


class PairingError(Exception):
    """Base for every pairing-flow-specific failure."""


class PairingCodeNotFound(PairingError):
    """No pairing code with this value was ever issued."""


class PairingCodeExpired(PairingError):
    """The code existed but its TTL has elapsed."""


class PairingCodeAlreadyUsed(PairingError):
    """The code existed and was valid, but has already been consumed --
    pairing codes are single-use."""
