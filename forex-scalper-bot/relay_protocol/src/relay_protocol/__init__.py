"""
relay_protocol -- the wire format spoken over the WebSocket connection
between the cloud-hosted forex-scalper-bot backend and the local MT4/5
connector running on a user's own machine (see
forex-scalper-bot/docs/ARCHITECTURE.md's Phase 5 section for the full
design this implements).

Deliberately dependency-free of both `fx_bot` and any MT5 package: this
package is imported by BOTH sides of the relay, including the
Windows-only connector process, which must never need the cloud
backend's dependency closure (FastAPI, SQLAlchemy, etc.) just to speak
the protocol its own process uses.
"""
from .envelope import Envelope, EnvelopeKind
from .methods import EventMethod, RequestMethod
from .wire_models import WireFill, WireMarketSnapshot, WireOrder, WirePosition

__all__ = [
    "Envelope",
    "EnvelopeKind",
    "RequestMethod",
    "EventMethod",
    "WireFill",
    "WireMarketSnapshot",
    "WireOrder",
    "WirePosition",
]
