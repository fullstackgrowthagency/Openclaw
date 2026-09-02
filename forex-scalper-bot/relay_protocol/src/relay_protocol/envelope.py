"""
The one frame shape every message on the connector's WebSocket takes,
JSON-encoded. See this project's Phase 5 plan ("Wire protocol" section)
for the full rationale -- summarized here:

- `request`/`response`/`error` are correlated by `id` (a `request`'s
  `id` is echoed back on exactly one `response` or `error`).
- `event` frames are one-way, connector-pushed, and carry no `id` --
  there's nothing to correlate a push notification against.
- `auth` is the first frame sent on every new socket, before anything
  else is accepted.

`v` is a schema version, bumped only on a breaking wire-format change --
not on every payload addition -- so a mismatched connector build can be
detected before it does anything more confusing than a KeyError deep in
a handler.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

WIRE_PROTOCOL_VERSION = 1

# WebSocket close code a relay server sends when an `auth` frame is
# missing, malformed, or carries an invalid/unrecognized token -- a
# connector seeing this specific code must stop auto-reconnecting and
# prompt for re-pairing rather than hot-looping, since retrying with the
# same rejected token can never succeed. Lives here (not on the cloud
# side alone) so both the cloud's RelayConnection and the connector's own
# RelayClient read one shared source of truth instead of two
# independently-maintained literals.
AUTH_FAILURE_CLOSE_CODE = 4401


class EnvelopeKind(str, Enum):
    REQUEST = "request"
    RESPONSE = "response"
    ERROR = "error"
    EVENT = "event"
    AUTH = "auth"


class Envelope(BaseModel):
    v: int = WIRE_PROTOCOL_VERSION
    # None only for `event` frames -- everything else correlates a
    # response/error back to the request that triggered it.
    id: Optional[str] = None
    kind: EnvelopeKind
    method: str
    payload: dict[str, Any] = Field(default_factory=dict)
    sent_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def make_request(cls, method: str, payload: Optional[dict] = None) -> "Envelope":
        return cls(id=str(uuid4()), kind=EnvelopeKind.REQUEST, method=method, payload=payload or {})

    @classmethod
    def make_response(cls, request_id: str, method: str, payload: Optional[dict] = None) -> "Envelope":
        return cls(id=request_id, kind=EnvelopeKind.RESPONSE, method=method, payload=payload or {})

    @classmethod
    def make_error(cls, request_id: str, method: str, *, error_type: str, message: str) -> "Envelope":
        return cls(
            id=request_id, kind=EnvelopeKind.ERROR, method=method,
            payload={"error_type": error_type, "message": message},
        )

    @classmethod
    def make_event(cls, method: str, payload: Optional[dict] = None) -> "Envelope":
        return cls(kind=EnvelopeKind.EVENT, method=method, payload=payload or {})

    @classmethod
    def make_auth(cls, *, token: str, account_id: str) -> "Envelope":
        return cls(
            id=str(uuid4()), kind=EnvelopeKind.AUTH, method="auth",
            payload={"token": token, "account_id": account_id},
        )

    def to_wire(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_wire(cls, raw: str) -> "Envelope":
        return cls.model_validate_json(raw)
