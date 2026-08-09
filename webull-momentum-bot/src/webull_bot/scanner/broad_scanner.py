"""
Tier 1: Broad Scanner.

Continuously screens a symbol universe down to candidates worth watching
closely. Cheap checks only (price range, float ceiling, basic dollar
volume) -- expensive per-tick metric work happens in CandidateWatcher.

The symbol universe itself (e.g. a premarket-gappers or most-active list)
is supplied by the caller rather than fetched here -- see data/universe.py.

Per-symbol checks run concurrently (a thread pool) rather than one at a
time: each symbol costs two network round-trips (a broker snapshot + a
float-data lookup), and with a universe that can now be 100+ symbols
(data/universe.py's MultiSourceUniverseProvider), sequential checks would
take long enough to meaningfully lag behind the rescan interval.

Webull's own sandbox rate limit (confirmed live 2026-08-08: sustained
~1 req/s regardless of concurrency -- see brokers/webull/retry.py's module
docstring for the full discovery) is enforced globally by
webull_market_data_limiter, not by capping `max_workers` here. That means
raising max_workers doesn't let this scanner exceed Webull's ceiling --
threads just queue on the limiter -- but it does let the FMP float lookup
for one symbol overlap with another symbol's (paced) Webull snapshot call,
which is where concurrency actually still buys wall-clock time now.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

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
    # Webull's own throughput ceiling is enforced globally by webull_market_data_limiter
    # (brokers/webull/retry.py), not by this value -- see module docstring. 10 just gives
    # enough overlap for FMP float lookups without spinning up needless idle threads.
    max_workers: int = 10


class BroadScanner:
    def __init__(self, broker: BrokerClient, float_provider: FloatDataProvider, config: BroadScannerConfig | None = None):
        self.broker = broker
        self.float_provider = float_provider
        self.config = config or BroadScannerConfig()

    def _check_symbol(self, symbol: str) -> Optional[Candidate]:
        try:
            snapshot = self.broker.get_snapshot(symbol)
        except Exception:
            return None

        if not (self.config.min_price <= snapshot.last_price <= self.config.max_price):
            return None

        dollar_volume = snapshot.last_price * snapshot.cumulative_volume
        if dollar_volume < self.config.min_dollar_volume:
            return None

        try:
            float_data = self.float_provider.get_float_data(symbol)
        except Exception:
            return None

        if float_data.free_float_shares > self.config.max_free_float_shares:
            return None

        candidate = new_candidate(symbol, now=snapshot.timestamp)
        candidate.float_data = float_data
        transition(candidate, CandidateState.WATCHING, now=snapshot.timestamp, reason="passed broad scanner filters")
        return candidate

    def scan(self, symbol_universe: list[str]) -> list[Candidate]:
        if not symbol_universe:
            return []

        discovered: list[Candidate] = []
        with ThreadPoolExecutor(max_workers=min(self.config.max_workers, len(symbol_universe))) as executor:
            for candidate in executor.map(self._check_symbol, symbol_universe):
                if candidate is not None:
                    discovered.append(candidate)
        return discovered
