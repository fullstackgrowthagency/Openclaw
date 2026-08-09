"""
Tier 1: Broad Scanner.

Continuously screens a symbol universe down to candidates worth watching
closely. Only two things are genuinely *structural* gates here -- price
range and free float -- both checked cheaply before any candidate object
gets built. Dollar volume and average volume are deliberately NOT gates:
a historically-quiet low-float stock suddenly seeing abnormal volume is
exactly the pattern this bot targets, so a low reading on either must not
disqualify a symbol. Both are still computed and attached to the Candidate
as informational context (Candidate.dollar_volume_today/average_daily_volume/
previous_day_volume) for scoring/diagnostics instead of a pass/fail cutoff.
Expensive per-tick metric work happens in CandidateWatcher, not here.

The symbol universe itself (e.g. a premarket-gappers or most-active list)
is supplied by the caller rather than fetched here -- see data/universe.py.

Per-symbol checks run concurrently (a thread pool) rather than one at a
time: each symbol can cost multiple network round-trips (broker snapshot,
float lookup, average-volume history, resistance bars), and with a
universe that can now be considerably larger than before (data/universe.py
now paginates instead of capping at 100 per source, and the price range
widened from $1-$20 to $0.40-$25), sequential checks would take long
enough to meaningfully lag behind the rescan interval.

Webull's own sandbox rate limit (confirmed live 2026-08-08: sustained
~1 req/s regardless of concurrency -- see brokers/webull/retry.py's module
docstring for the full discovery) is enforced globally by
webull_market_data_limiter, not by capping `max_workers` here. That means
raising max_workers doesn't let this scanner exceed Webull's ceiling --
threads just queue on the limiter -- but it does let the FMP float lookup
for one symbol overlap with another symbol's (paced) Webull snapshot call,
which is where concurrency actually still buys wall-clock time now.

Call ordering inside `_check_symbol` is deliberately cost-conscious: the
free-float check (still a hard structural gate) runs *before* the
average-volume/resistance network calls, so a symbol that's structurally
disqualified by float size never pays for those extra Webull round-trips.
`_compute_average_volume_info` and `_compute_static_resistance_levels`
both fail soft (never reject the candidate) for the same reason neither is
a gate: a missing/failed lookup just means less informational context on
the candidate, not a reason to discard it.
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
    # Structural gates: a symbol outside this price range, or with a free
    # float above the ceiling below, is never a candidate -- see the module
    # docstring for why dollar volume/average volume are NOT included here.
    min_price: float = 0.40
    max_price: float = 25.00
    max_free_float_shares: float = 20_000_000
    # How many days of daily-volume history to fetch for average_daily_volume
    # / previous_day_volume -- purely informational now (see module
    # docstring), not compared against any threshold here anymore. Matches
    # the relative_volume_10d convention used elsewhere (universe.py, MIS
    # scoring).
    avg_volume_lookback_days: int = 10
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

        # Structural gate: free float. Checked before the average-volume/
        # resistance network calls below so a symbol disqualified here never
        # pays for those extra round-trips (see module docstring).
        try:
            float_data = self.float_provider.get_float_data(symbol)
        except Exception:
            return None

        if float_data.free_float_shares > self.config.max_free_float_shares:
            return None

        candidate = new_candidate(symbol, now=snapshot.timestamp)
        candidate.float_data = float_data
        candidate.dollar_volume_today = snapshot.last_price * snapshot.cumulative_volume
        average_volume, previous_day_volume = self._compute_average_volume_info(symbol)
        candidate.average_daily_volume = average_volume
        candidate.previous_day_volume = previous_day_volume
        candidate.static_resistance_levels = self._compute_static_resistance_levels(symbol)
        transition(candidate, CandidateState.WATCHING, now=snapshot.timestamp, reason="passed broad scanner filters")
        return candidate

    def _compute_average_volume_info(self, symbol: str) -> tuple[Optional[float], Optional[float]]:
        """Returns (average_daily_volume, previous_day_volume) -- purely
        informational (see module docstring for why this no longer gates
        discovery). Returns (None, None) rather than raising on any failure
        or missing capability, same fail-soft contract as
        _compute_static_resistance_levels below: this enriches a candidate
        that's already been decided to be valid, it doesn't decide validity.

        Skipped for brokers with no real daily-volume history (paper/
        backtest mode) via getattr, same reasoning as
        WebullBrokerClient.get_daily_volumes's docstring: this isn't part
        of the BrokerClient interface since there's no real backing data in
        those modes."""
        get_daily_volumes = getattr(self.broker, "get_daily_volumes", None)
        if get_daily_volumes is None:
            return (None, None)
        try:
            volumes = get_daily_volumes(symbol, self.config.avg_volume_lookback_days)
        except Exception:
            return (None, None)
        if not volumes:
            return (None, None)
        average_volume = sum(volumes) / len(volumes)
        previous_day_volume = volumes[0]  # most-recent-first, per get_daily_volumes' contract
        return (average_volume, previous_day_volume)

    def _compute_static_resistance_levels(self, symbol: str) -> list[float]:
        """High-volume-node price levels from recent volume-profile
        analysis (see metrics/volume_profile.py's module docstring for why
        this is preferred over hand-picked special levels). Computed once
        at discovery, not refreshed on every tick -- like float_data above,
        this is a one-time enrichment of the candidate, not a per-snapshot
        recomputation.

        A failure or missing capability here returns an empty list rather
        than failing the candidate: this only affects how
        CandidateWatcher.update_resistance merges in static levels on top
        of the running high of day, it isn't a pass/fail discovery gate,
        so there's nothing to reject -- same fail-soft contract as
        _compute_average_volume_info above."""
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
