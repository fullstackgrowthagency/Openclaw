"""
Strategy interface. Mirrors webull-momentum-bot/src/webull_bot/interfaces/
strategy.py's contract exactly in spirit: a Strategy only ever reads market
state and emits Signal objects -- it never touches a broker/connector
directly (that wiring lives downstream, in the execution layer, after the
risk engine has approved a signal).

Signature is provisional pending the rule-builder phase's open question
(see the approved plan) of whether a state-machine/Candidate-equivalent
object is actually needed for scalping's much shorter hold times, or
whether straight per-tick evaluation against recent snapshot history is
enough. `history` is deliberately typed generically for now -- it may
become bar/indicator data once that design lands, not raw MarketSnapshots
forever.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..models import MarketSnapshot, Signal


class Strategy(ABC):
    name: str
    version: str

    @abstractmethod
    def on_snapshot(
        self, symbol: str, snapshot: MarketSnapshot, history: list[MarketSnapshot],
    ) -> Optional[Signal]:
        """Called on each new market snapshot for a tracked pair. `history`
        is the recent snapshot buffer for `symbol` (most-recent last),
        enough for the strategy to compute whatever indicators it needs.
        Return a Signal to request entry/exit/scale, or None."""
        ...
