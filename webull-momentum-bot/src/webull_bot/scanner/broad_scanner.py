"""
Tier 1: Broad Scanner.

Continuously screens a symbol universe down to candidates worth watching
closely. Structural gates here -- price range, free float, and a volume
floor -- are all checked cheaply before a candidate is fully built out.

Dollar volume (Candidate.dollar_volume_today) is still deliberately NOT a
gate: it's still just informational/scoring context. Average daily volume,
previous-day volume, and today's current volume-so-far, however, ARE a
gate again as of 2026-08-09 (a prior pass through this file had made
average/previous-day volume purely informational -- see git history/
ARCHITECTURE.md for that reasoning -- but per explicit user request
they're back to rejecting, with new, looser thresholds and a three-way
either-or exemption): a symbol is rejected only when ALL THREE of
average_daily_volume, previous_day_volume, AND today's current_day_volume
(snapshot.cumulative_volume -- already fetched for the price check above,
no extra round-trip) miss their respective BroadScannerConfig floors --
clearing any single one of the three alone is enough to survive. A missing
average_volume or previous_day_volume (None -- paper/backtest brokers, or
a failed lookup) can't be proven to miss its floor, so it's treated as NOT
failing -- meaning a broker with no daily-volume history at all can never
trigger this rejection regardless of current_day_volume, since "all three
fail" is then impossible. See _fails_volume_floor. This never rejects a
candidate for missing data, same fail-soft spirit as the free-float check
failing soft on a lookup error, just applied to a value comparison instead
of an exception.

Cost optimization (2026-08-09): the get_daily_volumes network call behind
average_volume/previous_day_volume is skipped entirely whenever
current_day_volume alone already clears min_current_day_volume, since the
either-or rule above means nothing that call could return would change
the outcome in that case -- see check_symbol_verbose.

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
free-float check runs first, then the volume-floor check right after
`_compute_average_volume_info`, and only THEN the resistance computation
(`_compute_static_resistance_levels`) -- the most expensive remaining call
-- so a symbol disqualified by float size or volume never pays for that
extra Webull round-trip. `_compute_static_resistance_levels` itself still
fails soft (never rejects): a missing/failed lookup there just means less
informational context on a candidate that's already been decided to be
valid, not a reason to discard it.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from ..interfaces.broker import BrokerClient
from ..interfaces.float_provider import FloatDataProvider
from ..metrics.opening_range import compute_opening_range_high
from ..metrics.volume_profile import compute_volume_profile, filter_bars_by_lookback, high_volume_node_levels
from ..models import Candidate
from ..state_machine import CandidateState, new_candidate, transition


@dataclass
class BroadScannerConfig:
    # Structural gates: a symbol outside this price range, with a free
    # float above the ceiling below, or that misses BOTH volume floors
    # below, is never a candidate -- see the module docstring for the
    # either-or exemption on the volume floors and why dollar volume is
    # NOT included here.
    min_price: float = 0.40
    max_price: float = 25.00
    max_free_float_shares: float = 20_000_000
    # Volume floor (see module docstring and _fails_volume_floor): a symbol
    # is rejected only when its average_daily_volume, previous_day_volume,
    # AND current_day_volume (today's volume-so-far) are ALL below their
    # respective floors below -- clearing any single one alone is enough to
    # survive. Unvalidated starting values per explicit user request
    # (2026-08-09), not backtested.
    min_average_daily_volume: float = 500_000
    min_previous_day_volume: float = 750_000
    min_current_day_volume: float = 500_000
    # How many days of daily-volume history to fetch for average_daily_volume
    # / previous_day_volume above. Matches the relative_volume_10d convention
    # used elsewhere (universe.py, MIS scoring).
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
    # Opening-range breakout reference (metrics/opening_range.py) -- the
    # high of the first N minutes of the regular session, computed from
    # the SAME raw bars fetched for the volume profile above, no extra
    # network call. See strategy/opening_range_breakout.py.
    opening_range_minutes: int = 5
    # Webull's own throughput ceiling is enforced globally by webull_market_data_limiter
    # (brokers/webull/retry.py), not by this value -- see module docstring. 10 just gives
    # enough overlap for FMP float lookups without spinning up needless idle threads.
    max_workers: int = 10


class BroadScanner:
    def __init__(
        self,
        broker: BrokerClient,
        float_provider: FloatDataProvider,
        config: BroadScannerConfig | None = None,
        *,
        now_fn: Callable[[], datetime] = datetime.utcnow,
    ):
        self.broker = broker
        self.float_provider = float_provider
        self.config = config or BroadScannerConfig()
        # Injectable for tests -- see _compute_opening_range_high, which
        # needs "now" to resolve today's market-open window. Defaults to
        # the real clock, same injection pattern as FMPFloatProvider's
        # http_get / YFinanceFloatProvider's info_fetcher.
        self.now_fn = now_fn

    def _check_symbol(self, symbol: str) -> Optional[Candidate]:
        """Used by scan()'s bulk per-cycle path, which only cares about the
        resulting candidate list, not why a rejected symbol was rejected --
        see check_symbol_verbose for the version that also explains a
        rejection, used by the dashboard's on-demand single-ticker scan."""
        candidate, _reason = self.check_symbol_verbose(symbol)
        return candidate

    def check_symbol_verbose(self, symbol: str) -> tuple[Optional[Candidate], Optional[str]]:
        """Runs the exact same structural checks as _check_symbol, but also
        returns a human-readable reason when the symbol is rejected (i.e.
        the candidate is None) -- for the dashboard's on-demand
        single-ticker scan (dashboard/app.py's POST /api/scan-symbol),
        where a human explicitly asked to check one symbol and wants to
        know why, unlike scan()'s bulk path which silently drops rejected
        symbols by the thousand every cycle."""
        try:
            snapshot = self.broker.get_snapshot(symbol)
        except Exception as exc:
            return None, f"Failed to fetch a market snapshot for {symbol}: {exc}"

        if not (self.config.min_price <= snapshot.last_price <= self.config.max_price):
            return None, (
                f"Price ${snapshot.last_price:.2f} is outside the allowed range "
                f"(${self.config.min_price:.2f}-${self.config.max_price:.2f})."
            )

        # Structural gate: free float. Checked before the average-volume/
        # resistance network calls below so a symbol disqualified here never
        # pays for those extra round-trips (see module docstring).
        try:
            float_data = self.float_provider.get_float_data(symbol)
        except Exception as exc:
            return None, f"Failed to fetch free-float data for {symbol}: {exc}"

        if float_data.free_float_shares > self.config.max_free_float_shares:
            return None, (
                f"Free float {float_data.free_float_shares:,.0f} exceeds the "
                f"{self.config.max_free_float_shares:,.0f}-share ceiling."
            )

        # Structural gate: volume floor. Checked before the (more
        # expensive) resistance network call below so a symbol disqualified
        # here never pays for that extra round-trip (see module docstring).
        #
        # get_daily_volumes is skipped entirely when current-day volume
        # alone already clears min_current_day_volume: rejection requires
        # ALL THREE floors to be missed (_fails_volume_floor), so nothing
        # average/previous-day volume could report would change the
        # outcome in that case -- the candidate survives regardless, and
        # it isn't worth an extra Webull round-trip to confirm that.
        # average_volume/previous_day_volume simply stay None on the
        # resulting candidate then, same fully-handled case as a broker
        # with no daily-volume history at all (paper/backtest mode).
        average_volume: Optional[float] = None
        previous_day_volume: Optional[float] = None
        if snapshot.cumulative_volume < self.config.min_current_day_volume:
            average_volume, previous_day_volume = self._compute_average_volume_info(symbol)
            if self._fails_volume_floor(average_volume, previous_day_volume, snapshot.cumulative_volume):
                return None, (
                    f"Average daily volume ({self._fmt_volume(average_volume)}), previous-day volume "
                    f"({self._fmt_volume(previous_day_volume)}), and today's volume-so-far "
                    f"({self._fmt_volume(snapshot.cumulative_volume)}) are all below their floors "
                    f"(min_average_daily_volume={self.config.min_average_daily_volume:,.0f}, "
                    f"min_previous_day_volume={self.config.min_previous_day_volume:,.0f}, "
                    f"min_current_day_volume={self.config.min_current_day_volume:,.0f})."
                )

        # Fetched once and shared between static_resistance_levels and
        # opening_range_high below -- both are derived from the same raw
        # bars, so there's no reason to pay for get_raw_bars twice.
        bars = self._fetch_raw_bars(symbol)

        candidate = new_candidate(symbol, now=snapshot.timestamp)
        candidate.float_data = float_data
        candidate.dollar_volume_today = snapshot.last_price * snapshot.cumulative_volume
        candidate.average_daily_volume = average_volume
        candidate.previous_day_volume = previous_day_volume
        candidate.static_resistance_levels = self._compute_static_resistance_levels(bars)
        candidate.opening_range_high = self._compute_opening_range_high(bars)
        candidate.resistance_last_refreshed_at = snapshot.timestamp
        transition(candidate, CandidateState.WATCHING, now=snapshot.timestamp, reason="passed broad scanner filters")
        return candidate, None

    @staticmethod
    def _fmt_volume(volume: Optional[float]) -> str:
        return f"{volume:,.0f}" if volume is not None else "unknown"

    def _fails_volume_floor(
        self, average_volume: Optional[float], previous_day_volume: Optional[float], current_day_volume: float
    ) -> bool:
        """True only when ALL THREE volume floors are missed -- clearing
        any single one of min_average_daily_volume, min_previous_day_volume,
        or min_current_day_volume is enough to survive (see module
        docstring/BroadScannerConfig). current_day_volume is
        snapshot.cumulative_volume -- always available (the caller already
        has the snapshot by this point) -- while average_volume/
        previous_day_volume can be None (paper/backtest brokers, or a
        failed lookup). A None value can't be proven to miss its floor, so
        it's treated as NOT failing rather than rejecting on missing data:
        this means a broker with no daily-volume history at all (average_volume
        and previous_day_volume always None) never rejects here regardless
        of current_day_volume, same fail-soft guarantee the two-floor
        version of this check always had."""
        avg_fails = average_volume is not None and average_volume < self.config.min_average_daily_volume
        previous_day_fails = (
            previous_day_volume is not None and previous_day_volume < self.config.min_previous_day_volume
        )
        current_day_fails = current_day_volume < self.config.min_current_day_volume
        return avg_fails and previous_day_fails and current_day_fails

    def _compute_average_volume_info(self, symbol: str) -> tuple[Optional[float], Optional[float]]:
        """Returns (average_daily_volume, previous_day_volume), the two
        values _fails_volume_floor above compares against
        BroadScannerConfig's floors. Returns (None, None) rather than
        raising on any failure or missing capability -- see
        _fails_volume_floor for why that means "don't reject" rather than
        "reject," same fail-soft contract as _compute_static_resistance_levels
        below.

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

    def _fetch_raw_bars(self, symbol: str) -> Optional[list[dict]]:
        """Returns None (not an empty list) on any failure or missing
        capability, so _compute_static_resistance_levels/
        _compute_opening_range_high below can tell "no bars available"
        apart from "bars fetched but empty" -- not that either currently
        treats them differently, but it keeps the fail-soft contract
        explicit rather than overloading an empty list with two meanings."""
        get_raw_bars = getattr(self.broker, "get_raw_bars", None)
        if get_raw_bars is None:
            return None
        try:
            return get_raw_bars(symbol, self.config.volume_profile_bar_interval, self.config.volume_profile_bar_count)
        except Exception:
            return None

    def _compute_static_resistance_levels(self, bars: Optional[list[dict]]) -> list[float]:
        """High-volume-node price levels from recent volume-profile
        analysis (see metrics/volume_profile.py's module docstring for why
        this is preferred over hand-picked special levels). Computed at
        discovery AND periodically re-computed for still-pre-entry
        candidates via refresh_resistance_levels below (see
        TradingLoop._rescan_universe) -- like float_data above, this
        started as a one-time enrichment, but a volume profile built from
        only the bars available at the moment of discovery is necessarily
        incomplete for a candidate found early in the session; refreshing
        it as the day's volume accumulates lets it reflect nodes that
        simply hadn't formed yet. `bars` is whatever _fetch_raw_bars
        returned (shared with _compute_opening_range_high on the discovery
        path, one network call for both -- the periodic refresh path only
        needs this one, not opening_range_high, see below).

        A failure or missing capability here returns an empty list rather
        than failing the candidate: this only affects how
        CandidateWatcher.update_resistance merges in static levels on top
        of the running high of day, it isn't a pass/fail discovery gate,
        so there's nothing to reject -- same fail-soft contract as
        _compute_average_volume_info above."""
        if not bars:
            return []
        recent_bars = filter_bars_by_lookback(bars, lookback_days=self.config.volume_profile_lookback_days)
        nodes = compute_volume_profile(recent_bars, num_buckets=self.config.volume_profile_num_buckets)
        return high_volume_node_levels(
            nodes,
            top_n=self.config.volume_profile_top_n_nodes,
            min_volume_pct_of_max=self.config.volume_profile_min_node_pct,
        )

    def refresh_resistance_levels(self, candidate: Candidate, *, now: Optional[datetime] = None) -> None:
        """Re-fetches bars and recomputes candidate.static_resistance_levels
        from scratch -- called periodically by TradingLoop._rescan_universe
        for already-tracked, pre-entry candidates (WATCHING/HEATING_UP/ARMED)
        so resistance stays accurate as the day's volume profile fills in,
        rather than being frozen at whatever bars existed the moment this
        candidate was first discovered (see _compute_static_resistance_levels
        above). Deliberately does NOT touch opening_range_high: that's a
        fixed historical window (the first opening_range_minutes of the
        session), and a later bars fetch can only see it WORSE, never
        better -- get_raw_bars' fixed bar count covers a trailing window
        that slides forward through the day, pushing market-open bars
        further out of range the later it's called (see
        metrics/opening_range.py and this module's docstring on
        get_raw_bars' data-sparsity behavior).

        On any fetch failure, leaves static_resistance_levels and
        resistance_last_refreshed_at both untouched (not cleared, not
        bumped) -- a transient failure this cycle shouldn't erase a
        previously-good set of levels or reset the throttle clock, since
        that would make the next rescan retry immediately instead of
        waiting out the normal interval."""
        bars = self._fetch_raw_bars(candidate.symbol)
        if bars is None:
            return
        candidate.static_resistance_levels = self._compute_static_resistance_levels(bars)
        candidate.resistance_last_refreshed_at = now or self.now_fn()

    def _compute_opening_range_high(self, bars: Optional[list[dict]]) -> Optional[float]:
        """The high of the first opening_range_minutes of the session, from
        the same bars as _compute_static_resistance_levels above -- see
        metrics/opening_range.py. Returns None (not a discovery-gate
        failure) when bars are unavailable or don't cover market open --
        same fail-soft contract as the rest of this enrichment step;
        strategy/opening_range_breakout.py simply never fires for a
        candidate whose opening_range_high is None."""
        if not bars:
            return None
        return compute_opening_range_high(bars, opening_range_minutes=self.config.opening_range_minutes, now=self.now_fn())

    def scan(self, symbol_universe: list[str]) -> list[Candidate]:
        if not symbol_universe:
            return []

        discovered: list[Candidate] = []
        with ThreadPoolExecutor(max_workers=min(self.config.max_workers, len(symbol_universe))) as executor:
            for candidate in executor.map(self._check_symbol, symbol_universe):
                if candidate is not None:
                    discovered.append(candidate)
        return discovered
