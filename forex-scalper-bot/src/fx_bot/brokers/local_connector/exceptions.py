"""
Exceptions specific to LocalConnectorBroker, extending -- not replacing --
PaperBrokerClient's existing NotImplementedError/KeyError conventions
(see that module's docstring). Exactly three concrete types, matching the
approved Phase 5 design one-for-one; a malformed/unrecognized wire enum
value (see wire_convert.py) deliberately raises a bare ValueError instead
of a fourth type here -- that's a protocol/version-mismatch bug, not a
runtime broker condition.
"""
from __future__ import annotations


class LocalConnectorError(Exception):
    """Base for every local-connector-specific BrokerClient failure."""


class ConnectorOfflineError(LocalConnectorError):
    """The relay socket is not connected -- caught before sending, or
    detected mid-request/mid-read. Fails fast; there is nothing to wait
    on."""


class ConnectorTimeoutError(LocalConnectorError):
    """The socket was (or should have been) fine, but no response arrived
    within the per-call deadline -- the connector itself is reachable but
    MT5 is hung, slow, or disconnected on its end."""


class BrokerRejectedError(LocalConnectorError):
    """A genuine MT5 trading rejection surfaced as an `error`-kind wire
    envelope -- a real business outcome (e.g. insufficient margin), not a
    connectivity fault. `error_type`/`message` are passed through exactly
    as the connector reported them; no attempt is made here to classify
    retryable vs. terminal rejections until real MT5 retcode vocabulary
    is known (see Phase 5g's manual verification checkpoint)."""

    def __init__(self, error_type: str, message: str):
        super().__init__(f"{error_type}: {message}")
        self.error_type = error_type
        self.message = message
