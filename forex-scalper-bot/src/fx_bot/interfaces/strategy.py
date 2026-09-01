"""
Strategy interface. Mirrors webull-momentum-bot/src/webull_bot/interfaces/
strategy.py's contract exactly in spirit: a Strategy only ever reads market
state and emits Signal objects -- it never touches a broker/connector
directly (that wiring lives downstream, in the execution layer, after the
risk engine has approved a signal).

RESOLVED (Phase 3): no state-machine/Candidate-equivalent object is used.
The equities bot's multi-state discovery pipeline (WATCHING -> HEATING_UP
-> ARMED -> CONFIRMING -> ...) exists to filter noise while scanning a
broad, constantly-changing universe of thousands of tickers. This bot has
no such discovery problem -- a StrategyConfig names exactly one pair (see
the approved plan's rule-builder schema), so there's nothing to scan or
narrow down. What a strategy DOES still need, without a full state
machine, is to know whether it's currently holding a position on its pair
-- that's the one piece of state threaded through below (`position`), not
reconstructed from a separate object. `history` is deliberately typed
generically -- indicators compute their own series from it (see
indicators/registry.py); it may become true OHLC bar data once bar
aggregation exists, not raw MarketSnapshots forever.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..models import MarketSnapshot, Position, Signal


class Strategy(ABC):
    name: str
    version: str

    @abstractmethod
    def on_snapshot(
        self, symbol: str, snapshot: MarketSnapshot, history: list[MarketSnapshot], position: Optional[Position],
    ) -> Optional[Signal]:
        """Called on each new market snapshot for a tracked pair. `history`
        is the recent snapshot buffer for `symbol` (most-recent last),
        enough for the strategy to compute whatever indicators it needs.
        `position` is the currently open position on `symbol`, if any --
        None means the strategy should be looking for an entry, not None
        means it should only ever emit EXIT (never a second entry) since
        RiskEngine.evaluate only gates entries and would reject a same-
        symbol double-entry anyway (see risk/risk_engine.py). Return a
        Signal to request entry/exit/scale, or None."""
        ...
