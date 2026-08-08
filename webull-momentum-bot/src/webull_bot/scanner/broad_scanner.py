"""
Tier 1: Broad Scanner.

Continuously screens a symbol universe down to candidates worth watching
closely. Cheap checks only (price range, float ceiling, basic dollar
volume) -- expensive per-tick metric work happens in CandidateWatcher.

The symbol universe itself (e.g. a premarket-gappers or most-active list)
is supplied by the caller rather than fetched here: which Webull endpoint
(or combination of endpoints) is the right source is a Phase 2 integration
decision that should be made against the current OpenAPI docs, not guessed.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..interfaces.broker import BrokerClient
from ..interfaces.float_provider import FloatDataProvider
from ..models import Candidate
from ..state_machine import CandidateState, new_candidate, transition


@dataclass
class BroadScannerConfig:
    min_price: float = 1.0
    max_price: float = 20.0
    max_free_float_shares: float = 20_000_000
    min_dollar_volume: float = 200_000


class BroadScanner:
    def __init__(self, broker: BrokerClient, float_provider: FloatDataProvider, config: BroadScannerConfig | None = None):
        self.broker = broker
        self.float_provider = float_provider
        self.config = config or BroadScannerConfig()

    def scan(self, symbol_universe: list[str]) -> list[Candidate]:
        discovered: list[Candidate] = []
        for symbol in symbol_universe:
            try:
                snapshot = self.broker.get_snapshot(symbol)
            except Exception:
                continue

            if not (self.config.min_price <= snapshot.last_price <= self.config.max_price):
                continue

            dollar_volume = snapshot.last_price * snapshot.cumulative_volume
            if dollar_volume < self.config.min_dollar_volume:
                continue

            try:
                float_data = self.float_provider.get_float_data(symbol)
            except Exception:
                continue

            if float_data.free_float_shares > self.config.max_free_float_shares:
                continue

            candidate = new_candidate(symbol, now=snapshot.timestamp)
            candidate.float_data = float_data
            transition(candidate, CandidateState.WATCHING, now=snapshot.timestamp, reason="passed broad scanner filters")
            discovered.append(candidate)

        return discovered
