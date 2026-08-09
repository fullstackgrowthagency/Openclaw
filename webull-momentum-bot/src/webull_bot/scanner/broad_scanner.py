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

The average-volume filter (`_passes_average_volume_filter`) adds a second
Webull-paced call (`get_daily_volumes`) for every symbol that clears the
price/dollar-volume checks above it, which meaningfully adds to per-scan
wall-clock time -- see runtime/trading_loop.py's TradingLoopConfig for the
measured per-symbol cost. There is no cap on how many universe symbols get
scanned (see TradingLoop._rescan_universe), so total scan time scales with
however many symbols the universe returns that cycle rather than a fixed
number. This filter is skipped for brokers without real daily-volume
history (paper/backtest) rather than rejecting everything in those modes.

`_compute_static_resistance_levels` adds a *third* Webull-paced call
(`get_raw_bars`, fetching intraday bars for volume-profile analysis --
see metrics/volume_profile.py) for every symbol that passes the filters
above and gets an actual Candidate built. Unlike the filters above, a
failure or missing capability here does not reject the candidate -- it
just falls back to plain running-high-of-day resistance tracking, since
this only enriches how resistance is tracked, it isn't a discovery gate.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

from ..interfaces.broker import BrokerClient
from ..interfaces.float_provider import FloatDataProvider
from ..metrics.volume_profile import compute_volume_profile, filter_bars_by_lookback, high_volume_node_levels
from ..models import Candidate
from ..state_machine import CandidateState, new_candidate, transition


@dataclass
class BroadScannerConfig:
    min_price: float = 1.0
    max_price: float = 20.0
    max_free_float_shares: float = 20_000_000
    min_dollar_volume: float = 200_000
    # A stock averaging under this many shares/day is normally too illiquid
    # for this strategy -- unless its previous trading day alone already
    # cleared the bar (see _passes_average_volume_filter), which is exactly
    # the "quiet float just woke up" pattern this bot targets. min_dollar_volume
    # above is today's-volume-so-far * price and doesn't capture either of
    # these on its own.
    min_average_volume: float = 1_000_000
    avg_volume_lookback_days: int = 10  # matches the relative_volume_10d convention used elsewhere (universe.py, MIS scoring)
    # Volume-profile resistance detection (metrics/volume_profile.py). 780
    # 5-minute bars is ~10 trading days of continuous coverage for a liquid
    # name; for an illiquid one, Webull returns however far back it has to
    # reach to find 780 bars with real data (confirmed live 2026-08-09 this
    # can be months) -- volume_profile_lookback_days then trims that back
    # down to a bounded, *recent* calendar window before the profile is
    # built, so ancient price action doesn't define today's resistance.
    volume_profile_bar_interval: str = "5m"
    volume_profile_bar_count: int = 780
    volume_profile_lookback_days: int = 20
    volume_profile_num_buckets: int = 50
    volume_profile_top_n_nodes: int = 5
    # Fraction of the single largest volume cluster's volume a bucket needs
    # to count as a real high-volume node rather than background noise --
    # unvalidated starting point, same spirit as scoring/weights.yaml.
    volume_profile_min_node_pct: float = 0.3
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

        if not self._passes_average_volume_filter(symbol):
            return None

        try:
            float_data = self.float_provider.get_float_data(symbol)
        except Exception:
            return None

        if float_data.free_float_shares > self.config.max_free_float_shares:
            return None

        candidate = new_candidate(symbol, now=snapshot.timestamp)
        candidate.float_data = float_data
        candidate.static_resistance_levels = self._compute_static_resistance_levels(symbol)
        transition(candidate, CandidateState.WATCHING, now=snapshot.timestamp, reason="passed broad scanner filters")
        return candidate

    def _passes_average_volume_filter(self, symbol: str) -> bool:
        """Reject a stock trading under min_average_volume shares/day on
        average, UNLESS the previous trading day alone already cleared that
        bar -- a previously-quiet float that just had one big volume day is
        exactly the pattern this bot targets, and shouldn't be excluded
        just because a longer average hasn't caught up to it yet.

        Skipped (returns True -- doesn't filter anything out) for brokers
        with no real daily-volume history: paper/backtest mode has nothing
        to check this against, so this uses getattr rather than requiring
        every BrokerClient implementation to support it -- see
        WebullBrokerClient.get_daily_volumes's docstring for why this isn't
        part of the BrokerClient interface."""
        get_daily_volumes = getattr(self.broker, "get_daily_volumes", None)
        if get_daily_volumes is None:
            return True
        try:
            volumes = get_daily_volumes(symbol, self.config.avg_volume_lookback_days)
        except Exception:
            return False  # consistent with get_snapshot/float_provider above: a failed lookup rejects, it doesn't pass through
        if not volumes:
            return True
        average_volume = sum(volumes) / len(volumes)
        previous_day_volume = volumes[0]  # most-recent-first, per get_daily_volumes' contract
        return average_volume >= self.config.min_average_volume or previous_day_volume > self.config.min_average_volume

    def _compute_static_resistance_levels(self, symbol: str) -> list[float]:
        """High-volume-node price levels from recent volume-profile
        analysis (see metrics/volume_profile.py's module docstring for why
        this is preferred over hand-picked special levels). Computed once
        at discovery, not refreshed on every tick -- like float_data above,
        this is a one-time enrichment of the candidate, not a per-snapshot
        recomputation.

        Unlike _passes_average_volume_filter, a failure or missing
        capability here returns an empty list rather than failing the
        candidate: this only affects how CandidateWatcher.update_resistance
        merges in static levels on top of the running high of day, it
        isn't a pass/fail discovery gate, so there's nothing to reject."""
        get_raw_bars = getattr(self.broker, "get_raw_bars", None)
        if get_raw_bars is None:
            return []
        try:
            bars = get_raw_bars(symbol, self.config.volume_profile_bar_interval, self.config.volume_profile_bar_count)
        except Exception:
            return []
        recent_bars = filter_bars_by_lookback(bars, lookback_days=self.config.volume_profile_lookback_days)
        nodes = compute_volume_profile(recent_bars, num_buckets=self.config.volume_profile_num_buckets)
        return high_volume_node_levels(
            nodes,
            top_n=self.config.volume_profile_top_n_nodes,
            min_volume_pct_of_max=self.config.volume_profile_min_node_pct,
        )

    def scan(self, symbol_universe: list[str]) -> list[Candidate]:
        if not symbol_universe:
            return []

        discovered: list[Candidate] = []
        with ThreadPoolExecutor(max_workers=min(self.config.max_workers, len(symbol_universe))) as executor:
            for candidate in executor.map(self._check_symbol, symbol_universe):
                if candidate is not None:
                    discovered.append(candidate)
        return discovered
