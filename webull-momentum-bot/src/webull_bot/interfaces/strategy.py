"""
Strategy interface. A Strategy only ever reads candidate/market state and
emits Signal objects -- it has no reference to a broker and cannot place
orders. That wiring lives in execution/order_manager.py, downstream of the
risk engine. See docs/ARCHITECTURE.md for the full data-flow diagram.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..models import Candidate, MarketSnapshot, Signal


class Strategy(ABC):
    name: str
    version: str

    @abstractmethod
    def on_snapshot(self, candidate: Candidate, snapshot: MarketSnapshot) -> Optional[Signal]:
        """Called on each new market snapshot for an ARMED (or already-entered)
        candidate. Return a Signal to request entry/exit/scale, or None."""
        ...
