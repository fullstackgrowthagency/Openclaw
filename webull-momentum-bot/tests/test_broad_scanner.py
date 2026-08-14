"""
Tests for BroadScanner, including its concurrent per-symbol checking
(scanner/broad_scanner.py). Uses fakes with an artificial delay to prove
the thread pool actually runs checks in parallel, not just that results are
correct -- correctness alone wouldn't catch a regression back to sequential
scanning.
"""
import time
from datetime import datetime

import pytest

from webull_bot.enums import CandidateState
from webull_bot.interfaces.broker import BrokerClient
from webull_bot.interfaces.float_provider import FloatDataProvider
from webull_bot.models import FloatData, MarketSnapshot
from webull_bot.scanner.broad_scanner import BroadScanner, BroadScannerConfig


def _snapshot(symbol, price=5.0, cumulative_volume=200_000) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol, timestamp=datetime.utcnow(), last_price=price, bid=price - 0.01, ask=price + 0.01,
        bid_size=100, ask_size=100, cumulative_volume=cumulative_volume, vwap=price, high_of_day=price,
        low_of_day=price, open_price=price,
    )


def _float_data(symbol, free_float=3_000_000) -> FloatData:
    return FloatData(
        symbol=symbol, free_float_shares=free_float, shares_outstanding=free_float * 1.3,
        market_cap=None, float_percent=None, effective_date=None, fetched_at=datetime.utcnow(),
    )


class _SlowFakeBroker(BrokerClient):
    """Every get_snapshot() call sleeps -- used to prove concurrency reduces wall-clock time."""

    def __init__(self, delay_seconds: float, prices: dict[str, float]):
        self.delay_seconds = delay_seconds
        self.prices = prices

    def connect(self): pass
    def disconnect(self): pass
    def get_account_equity(self): return 25_000.0
    def get_buying_power(self): return 25_000.0
    def get_positions(self): return []
    def get_bars(self, symbol, interval, lookback): raise NotImplementedError
    def subscribe_quotes(self, symbols, on_update): raise NotImplementedError
    def unsubscribe_quotes(self, symbols): raise NotImplementedError
    def place_order(self, order): raise NotImplementedError
    def cancel_order(self, broker_order_id): raise NotImplementedError
    def modify_order(self, broker_order_id, **changes): raise NotImplementedError
    def get_order_status(self, broker_order_id): raise NotImplementedError
    def poll_fills(self, since=None): return []

    def get_snapshot(self, symbol):
        time.sleep(self.delay_seconds)
        return _snapshot(symbol, price=self.prices.get(symbol, 5.0))

    @property
    def is_live(self): return False


class _FakeFloatProvider(FloatDataProvider):
    def __init__(self, free_floats: dict[str, float] = None):
        self.free_floats = free_floats or {}

    def get_float_data(self, symbol):
        return _float_data(symbol, self.free_floats.get(symbol, 3_000_000))

    def get_float_data_bulk(self, symbols):
        return {s: self.get_float_data(s) for s in symbols}


def test_scan_runs_symbols_concurrently_not_sequentially():
    symbols = [f"SYM{i}" for i in range(10)]
    broker = _SlowFakeBroker(delay_seconds=0.1, prices={s: 5.0 for s in symbols})
    scanner = BroadScanner(broker, _FakeFloatProvider(), BroadScannerConfig(max_workers=5))

    start = time.monotonic()
    candidates = scanner.scan(symbols)
    elapsed = time.monotonic() - start

    assert len(candidates) == 10
    # Sequential would take ~1.0s (10 * 0.1s); with 5 workers it should take
    # roughly 2 batches (~0.2s). Generous bound to avoid CI flakiness.
    assert elapsed < 0.6, f"scan took {elapsed:.2f}s -- looks sequential, not concurrent"


# -- batched get_snapshots for the whole universe -- see
# WebullBrokerClient.get_snapshots' docstring for why this exists: every
# get_snapshot-family call shares the same globally-paced rate limiter, so
# fetching the universe one symbol at a time is what actually determines how
# long a full scan takes once the universe is large, not max_workers --------

class _BatchAwareSlowFakeBroker(_SlowFakeBroker):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.batch_calls = []
        self.individual_calls = []
        self.batch_priorities = []

    def get_snapshot(self, symbol):
        self.individual_calls.append(symbol)
        return super().get_snapshot(symbol)

    def get_snapshots(self, symbols, priority=None):
        self.batch_calls.append(list(symbols))
        self.batch_priorities.append(priority)
        return {s: _snapshot(s, price=self.prices.get(s, 5.0)) for s in symbols}


def test_scan_uses_one_batched_call_for_the_whole_universe():
    symbols = [f"SYM{i}" for i in range(10)]
    broker = _BatchAwareSlowFakeBroker(delay_seconds=0.0, prices={s: 5.0 for s in symbols})
    scanner = BroadScanner(broker, _FakeFloatProvider())

    candidates = scanner.scan(symbols)

    assert len(candidates) == 10
    assert broker.batch_calls == [symbols]
    assert broker.individual_calls == []  # no per-symbol fallback needed


def test_scan_requests_background_priority_for_its_batch_snapshot_call():
    # Discovery is never exit-critical -- must not contend with
    # TradingLoop's own CRITICAL-priority per-tick batch for MANAGING
    # positions. See retry.py's CallPriority docstring.
    from webull_bot.brokers.webull.retry import CallPriority

    symbols = [f"SYM{i}" for i in range(3)]
    broker = _BatchAwareSlowFakeBroker(delay_seconds=0.0, prices={s: 5.0 for s in symbols})
    scanner = BroadScanner(broker, _FakeFloatProvider())

    scanner.scan(symbols)

    assert broker.batch_priorities == [CallPriority.BACKGROUND]


def test_scan_falls_back_without_get_snapshots():
    # _SlowFakeBroker has no get_snapshots at all -- representative of
    # PaperBrokerClient/any broker that doesn't support batching. Already
    # implicitly covered by every other test in this file using
    # _SlowFakeBroker, but asserted explicitly here for clarity.
    symbols = [f"SYM{i}" for i in range(5)]
    broker = _SlowFakeBroker(delay_seconds=0.0, prices={s: 5.0 for s in symbols})
    scanner = BroadScanner(broker, _FakeFloatProvider())

    candidates = scanner.scan(symbols)

    assert len(candidates) == 5


def test_scan_falls_back_per_symbol_when_batch_call_raises():
    class _FlakyBatchBroker(_BatchAwareSlowFakeBroker):
        def get_snapshots(self, symbols, priority=None):
            self.batch_calls.append(list(symbols))
            raise RuntimeError("simulated Webull batch failure")

    symbols = [f"SYM{i}" for i in range(5)]
    broker = _FlakyBatchBroker(delay_seconds=0.0, prices={s: 5.0 for s in symbols})
    scanner = BroadScanner(broker, _FakeFloatProvider())

    candidates = scanner.scan(symbols)

    assert len(candidates) == 5  # every symbol still got discovered
    assert len(broker.batch_calls) == 1
    assert sorted(broker.individual_calls) == symbols


def test_scan_filters_price_and_free_float():
    symbols = ["CHEAP", "GOOD", "EXPENSIVE", "BIGFLOAT"]
    broker = _SlowFakeBroker(delay_seconds=0.0, prices={"CHEAP": 0.20, "GOOD": 5.0, "EXPENSIVE": 30.0, "BIGFLOAT": 5.0})
    float_provider = _FakeFloatProvider({"BIGFLOAT": 50_000_000})
    scanner = BroadScanner(broker, float_provider)

    candidates = scanner.scan(symbols)
    assert [c.symbol for c in candidates] == ["GOOD"]
    assert candidates[0].state == CandidateState.WATCHING


def test_scan_accepts_price_at_lower_boundary():
    broker = _SlowFakeBroker(delay_seconds=0.0, prices={"EDGE": 0.40})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    assert [c.symbol for c in scanner.scan(["EDGE"])] == ["EDGE"]


def test_scan_accepts_price_at_upper_boundary():
    broker = _SlowFakeBroker(delay_seconds=0.0, prices={"EDGE": 25.00})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    assert [c.symbol for c in scanner.scan(["EDGE"])] == ["EDGE"]


def test_scan_rejects_price_just_below_lower_boundary():
    broker = _SlowFakeBroker(delay_seconds=0.0, prices={"EDGE": 0.39})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    assert scanner.scan(["EDGE"]) == []


def test_scan_rejects_price_just_above_upper_boundary():
    broker = _SlowFakeBroker(delay_seconds=0.0, prices={"EDGE": 25.01})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    assert scanner.scan(["EDGE"]) == []


def test_scan_rejects_free_float_above_ceiling():
    broker = _SlowFakeBroker(delay_seconds=0.0, prices={"BIGFLOAT": 5.0})
    float_provider = _FakeFloatProvider({"BIGFLOAT": 20_000_001})
    scanner = BroadScanner(broker, float_provider)
    assert scanner.scan(["BIGFLOAT"]) == []


def test_scan_accepts_free_float_at_ceiling():
    broker = _SlowFakeBroker(delay_seconds=0.0, prices={"ATCEILING": 5.0})
    float_provider = _FakeFloatProvider({"ATCEILING": 20_000_000})
    scanner = BroadScanner(broker, float_provider)
    assert [c.symbol for c in scanner.scan(["ATCEILING"])] == ["ATCEILING"]


def test_scan_skips_symbol_on_broker_error_without_failing_others():
    class _FlakyBroker(_SlowFakeBroker):
        def get_snapshot(self, symbol):
            if symbol == "BROKEN":
                raise RuntimeError("simulated broker failure")
            return super().get_snapshot(symbol)

    broker = _FlakyBroker(delay_seconds=0.0, prices={"BROKEN": 5.0, "FINE": 5.0})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    candidates = scanner.scan(["BROKEN", "FINE"])
    assert [c.symbol for c in candidates] == ["FINE"]


def test_scan_skips_symbol_on_float_provider_error():
    class _FlakyFloatProvider(_FakeFloatProvider):
        def get_float_data(self, symbol):
            if symbol == "NOFLOAT":
                raise RuntimeError("simulated FMP failure")
            return super().get_float_data(symbol)

    broker = _SlowFakeBroker(delay_seconds=0.0, prices={"NOFLOAT": 5.0, "FINE": 5.0})
    scanner = BroadScanner(broker, _FlakyFloatProvider())
    candidates = scanner.scan(["NOFLOAT", "FINE"])
    assert [c.symbol for c in candidates] == ["FINE"]


def test_scan_empty_universe_returns_empty_without_error():
    scanner = BroadScanner(_SlowFakeBroker(0.0, {}), _FakeFloatProvider())
    assert scanner.scan([]) == []


def test_scan_does_not_reject_on_low_dollar_volume():
    # Dollar volume is informational only now -- see broad_scanner.py's
    # module docstring for why a low reading must not disqualify a symbol
    # (a historically-quiet low-float stock waking up is the target pattern).
    class _VolumeAwareBroker(_SlowFakeBroker):
        def get_snapshot(self, symbol):
            volume = 1_000 if symbol == "LOWVOL" else 200_000
            return _snapshot(symbol, price=5.0, cumulative_volume=volume)

    scanner = BroadScanner(_VolumeAwareBroker(0.0, {}), _FakeFloatProvider())
    candidates = scanner.scan(["LOWVOL", "HIGHVOL"])
    assert sorted(c.symbol for c in candidates) == ["HIGHVOL", "LOWVOL"]


def test_scan_records_dollar_volume_today_on_the_candidate():
    broker = _SlowFakeBroker(delay_seconds=0.0, prices={"SYM": 5.0})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    candidates = scanner.scan(["SYM"])
    assert candidates[0].dollar_volume_today == 5.0 * 200_000  # _snapshot()'s default cumulative_volume


class _DailyVolumeAwareBroker(_SlowFakeBroker):
    """Adds get_daily_volumes -- a capability real WebullBrokerClient has
    but _SlowFakeBroker (standing in for paper/backtest) does not, since
    _compute_average_volume_info is meant to be a no-op without it.
    cumulative_volumes optionally overrides the default 200,000 per-symbol
    current-day volume from _snapshot(), for exercising
    _fails_volume_floor's third (current-day) exemption."""

    def __init__(self, daily_volumes: dict[str, list[float]], cumulative_volumes: dict[str, float] | None = None):
        super().__init__(delay_seconds=0.0, prices={})
        self.daily_volumes = daily_volumes
        self.cumulative_volumes = cumulative_volumes or {}
        self.get_daily_volumes_calls: list[str] = []

    def get_daily_volumes(self, symbol, lookback_days, priority=None):
        self.get_daily_volumes_calls.append(symbol)
        return self.daily_volumes[symbol]

    def get_snapshot(self, symbol):
        return _snapshot(symbol, cumulative_volume=self.cumulative_volumes.get(symbol, 200_000))


def test_scan_keeps_candidate_exactly_at_average_volume_floor():
    # 500,000 is the default min_average_daily_volume floor -- AT it, not
    # below it, so this alone is enough to survive regardless of
    # previous_day_volume (also 500,000 here, below its own 750,000 floor).
    broker = _DailyVolumeAwareBroker({"QUIET": [500_000] * 10})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    candidates = scanner.scan(["QUIET"])
    assert [c.symbol for c in candidates] == ["QUIET"]
    assert candidates[0].average_daily_volume == 500_000
    assert candidates[0].previous_day_volume == 500_000


# -- volume floor (min_average_daily_volume / min_previous_day_volume /
# min_current_day_volume) ----------------------------------------------------
#
# Rejects only when ALL THREE are missed -- clearing any single one alone
# is enough to survive (see broad_scanner.py's module docstring/
# BroadScannerConfig). _DailyVolumeAwareBroker defaults current-day volume
# (cumulative_volume) to 200,000, below the 500,000 floor, in every test
# below unless a test explicitly overrides it -- so tests written before
# the current-day exemption existed still exercise "all three below."

def test_scan_rejects_when_all_three_volume_floors_are_missed():
    broker = _DailyVolumeAwareBroker({"DEAD": [400_000] * 10})  # current-day defaults to 200,000
    scanner = BroadScanner(broker, _FakeFloatProvider())
    assert scanner.scan(["DEAD"]) == []


def test_scan_keeps_candidate_when_only_average_volume_clears_its_floor():
    # previous_day_volume (400,000, the most-recent-first first element)
    # misses min_previous_day_volume (750,000); average_daily_volume
    # (~670,000 across all 10 days) clears min_average_daily_volume
    # (500,000) -- either one clearing is enough, so this survives.
    broker = _DailyVolumeAwareBroker({"OK": [400_000] + [700_000] * 9})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    candidates = scanner.scan(["OK"])
    assert [c.symbol for c in candidates] == ["OK"]
    assert candidates[0].average_daily_volume == pytest.approx((400_000 + 700_000 * 9) / 10)
    assert candidates[0].previous_day_volume == 400_000


def test_scan_keeps_candidate_when_only_previous_day_volume_clears_its_floor():
    # average_daily_volume (300,000) misses min_average_daily_volume
    # (500,000); previous_day_volume (800,000) clears min_previous_day_volume
    # (750,000) -- surviving on the other bar alone.
    broker = _DailyVolumeAwareBroker({"OK": [800_000] + [200_000] * 9})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    candidates = scanner.scan(["OK"])
    assert [c.symbol for c in candidates] == ["OK"]
    assert candidates[0].previous_day_volume == 800_000


def test_scan_keeps_candidate_exactly_at_previous_day_volume_floor():
    # 750,000 is the default min_previous_day_volume floor -- AT it, not
    # below it, so this alone is enough to survive regardless of a
    # below-floor average_daily_volume (also 400,000 here).
    broker = _DailyVolumeAwareBroker({"EDGE": [750_000] + [400_000] * 9})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    assert [c.symbol for c in scanner.scan(["EDGE"])] == ["EDGE"]


def test_scan_keeps_candidate_when_only_current_day_volume_clears_its_floor():
    # current_day_volume (600,000) clears min_current_day_volume (500,000)
    # alone -- daily_volumes is set up so average/previous-day would BOTH
    # miss their own floors if fetched, but get_daily_volumes should never
    # even be called here (see the dedicated skip test below), so this
    # candidate survives purely on current-day volume.
    broker = _DailyVolumeAwareBroker({"HOT": [400_000] * 10}, cumulative_volumes={"HOT": 600_000})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    assert [c.symbol for c in scanner.scan(["HOT"])] == ["HOT"]


def test_scan_keeps_candidate_exactly_at_current_day_volume_floor():
    # 500,000 is the default min_current_day_volume floor -- AT it, not
    # below it, so this alone is enough to survive without ever needing
    # average/previous-day volume.
    broker = _DailyVolumeAwareBroker({"EDGE": [400_000] * 10}, cumulative_volumes={"EDGE": 500_000})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    assert [c.symbol for c in scanner.scan(["EDGE"])] == ["EDGE"]


def test_scan_skips_daily_volume_lookup_when_current_day_volume_already_clears_its_floor():
    # Cost optimization: since clearing any single one of the three volume
    # floors is enough to survive, get_daily_volumes (a real network call)
    # is entirely unnecessary once current_day_volume alone already clears
    # min_current_day_volume -- nothing it could return would change the
    # outcome. Confirms the call itself is skipped, not just that the
    # candidate happens to survive either way.
    broker = _DailyVolumeAwareBroker({"HOT": [400_000] * 10}, cumulative_volumes={"HOT": 600_000})
    scanner = BroadScanner(broker, _FakeFloatProvider())

    candidates = scanner.scan(["HOT"])

    assert broker.get_daily_volumes_calls == []
    assert candidates[0].average_daily_volume is None
    assert candidates[0].previous_day_volume is None


def test_scan_still_calls_daily_volume_lookup_when_current_day_volume_does_not_clear_its_floor():
    # The skip only applies when current-day volume alone already clears
    # its bar -- when it doesn't, average/previous-day volume is still the
    # candidate's only remaining path to survival, so the call must happen.
    broker = _DailyVolumeAwareBroker({"OK": [800_000] * 10}, cumulative_volumes={"OK": 200_000})
    scanner = BroadScanner(broker, _FakeFloatProvider())

    candidates = scanner.scan(["OK"])

    assert broker.get_daily_volumes_calls == ["OK"]
    assert candidates[0].average_daily_volume == 800_000


def test_scan_rejects_when_current_day_volume_is_just_below_its_floor():
    broker = _DailyVolumeAwareBroker({"DEAD": [400_000] * 10}, cumulative_volumes={"DEAD": 499_999})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    assert scanner.scan(["DEAD"]) == []


def test_scan_does_not_reject_symbol_when_volume_data_is_missing_on_one_side():
    # A missing/failed lookup means (None, None) from _compute_average_volume_info
    # (see the daily-volume-lookup-failure and no-get_daily_volumes tests
    # below) -- a None can't be proven to miss its floor, so "all three
    # fail" becomes impossible regardless of current_day_volume, and this
    # must not reject.
    broker = _SlowFakeBroker(delay_seconds=0.0, prices={"ANY": 5.0})  # no get_daily_volumes at all
    scanner = BroadScanner(broker, _FakeFloatProvider())
    assert [c.symbol for c in scanner.scan(["ANY"])] == ["ANY"]


def test_scan_does_not_reject_symbol_on_daily_volume_lookup_failure():
    class _FlakyDailyVolumeBroker(_DailyVolumeAwareBroker):
        def get_daily_volumes(self, symbol, lookback_days, priority=None):
            raise RuntimeError("simulated Webull failure")

    broker = _FlakyDailyVolumeBroker({})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    candidates = scanner.scan(["ANY"])
    # Unlike a real discovery gate, a failed lookup must not reject the
    # candidate -- it's informational enrichment, not a pass/fail check.
    assert [c.symbol for c in candidates] == ["ANY"]
    assert candidates[0].average_daily_volume is None
    assert candidates[0].previous_day_volume is None


def test_scan_leaves_average_volume_info_none_without_get_daily_volumes():
    # _SlowFakeBroker has no get_daily_volumes at all -- representative of
    # PaperBrokerClient, which has no real daily-volume history at all.
    broker = _SlowFakeBroker(delay_seconds=0.0, prices={"ANY": 5.0})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    candidates = scanner.scan(["ANY"])
    assert [c.symbol for c in candidates] == ["ANY"]
    assert candidates[0].average_daily_volume is None
    assert candidates[0].previous_day_volume is None


class _RawBarsAwareBroker(_SlowFakeBroker):
    """Adds get_raw_bars -- a capability real WebullBrokerClient has but
    _SlowFakeBroker (standing in for paper/backtest) does not, since
    _compute_static_resistance_levels is meant to be a no-op without it."""

    def __init__(self, raw_bars: dict[str, list[dict]], prices: dict[str, float] = None):
        super().__init__(delay_seconds=0.0, prices=prices or {})
        self.raw_bars = raw_bars

    def get_raw_bars(self, symbol, interval, count, priority=None):
        return self.raw_bars[symbol]


def _bar(low, high, volume, time="2026-08-01T12:00:00.000+0000", close=None):
    # close defaults to the midpoint of [low, high] -- only
    # seed_history_from_bars (_compute_seed_snapshots) reads "close"; the
    # resistance/opening-range/volume-baseline tests below never touch it,
    # so an unspecified close just needs to be a plausible, present value.
    resolved_close = close if close is not None else (low + high) / 2
    return {"time": time, "low": str(low), "high": str(high), "close": str(resolved_close), "volume": str(volume)}


def test_scan_populates_static_resistance_levels_from_volume_profile():
    # A clear volume cluster around 8-9 (out of a broader 5-10 range) should
    # surface as a high-volume node in the candidate's static levels.
    bars = [_bar(5, 10, 100), _bar(8, 9, 5000), _bar(8, 9, 5000)]
    broker = _RawBarsAwareBroker({"CLUSTERED": bars}, prices={"CLUSTERED": 5.0})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    candidates = scanner.scan(["CLUSTERED"])
    assert len(candidates) == 1
    assert candidates[0].static_resistance_levels
    assert any(8 <= level <= 9 for level in candidates[0].static_resistance_levels)


def test_scan_leaves_static_resistance_levels_empty_on_raw_bars_failure():
    class _FlakyRawBarsBroker(_RawBarsAwareBroker):
        def get_raw_bars(self, symbol, interval, count, priority=None):
            raise RuntimeError("simulated Webull failure")

    broker = _FlakyRawBarsBroker({}, prices={"ANY": 5.0})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    candidates = scanner.scan(["ANY"])
    # Unlike the average-volume filter, a failed lookup here must NOT
    # reject the candidate -- it's an enrichment, not a discovery gate.
    assert len(candidates) == 1
    assert candidates[0].static_resistance_levels == []


def test_scan_leaves_static_resistance_levels_empty_without_get_raw_bars():
    # _SlowFakeBroker has no get_raw_bars at all -- representative of
    # PaperBrokerClient.
    broker = _SlowFakeBroker(delay_seconds=0.0, prices={"ANY": 5.0})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    candidates = scanner.scan(["ANY"])
    assert len(candidates) == 1
    assert candidates[0].static_resistance_levels == []


# -- opening_range_high (shares the same raw bars as static_resistance_levels,
# see metrics/opening_range.py and strategy/opening_range_breakout.py) -------

def test_scan_populates_opening_range_high_from_raw_bars():
    # 2026-08-03 is EDT (UTC-4) -- 9:30am ET == 13:30 UTC. Bar at 13:32 UTC
    # falls inside the default 5-minute opening range.
    bars = [_bar(5, 6.5, 1000, time="2026-08-03T13:32:00.000+0000")]
    broker = _RawBarsAwareBroker({"OPEN": bars}, prices={"OPEN": 5.0})
    scanner = BroadScanner(broker, _FakeFloatProvider(), now_fn=lambda: datetime(2026, 8, 3, 15, 0, 0))

    candidates = scanner.scan(["OPEN"])

    assert candidates[0].opening_range_high == 6.5


def test_scan_leaves_opening_range_high_none_when_bars_predate_market_open():
    # _bar()'s default time (12:00 UTC) is before 13:30 UTC market open on
    # that date, so it should never count towards the opening range.
    bars = [_bar(5, 6.5, 1000)]
    broker = _RawBarsAwareBroker({"ANY": bars}, prices={"ANY": 5.0})
    scanner = BroadScanner(broker, _FakeFloatProvider(), now_fn=lambda: datetime(2026, 8, 3, 15, 0, 0))

    candidates = scanner.scan(["ANY"])

    assert candidates[0].opening_range_high is None


def test_scan_leaves_opening_range_high_none_without_get_raw_bars():
    broker = _SlowFakeBroker(delay_seconds=0.0, prices={"ANY": 5.0})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    candidates = scanner.scan(["ANY"])
    assert candidates[0].opening_range_high is None


def test_scan_leaves_opening_range_high_none_on_raw_bars_failure():
    class _FlakyRawBarsBroker(_RawBarsAwareBroker):
        def get_raw_bars(self, symbol, interval, count, priority=None):
            raise RuntimeError("simulated Webull failure")

    broker = _FlakyRawBarsBroker({}, prices={"ANY": 5.0})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    candidates = scanner.scan(["ANY"])
    assert len(candidates) == 1
    assert candidates[0].opening_range_high is None


def test_scan_fetches_raw_bars_only_once_per_symbol():
    # static_resistance_levels and opening_range_high both derive from raw
    # bars -- get_raw_bars must be called once per symbol, not twice.
    class _CountingRawBarsBroker(_RawBarsAwareBroker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.call_count = 0

        def get_raw_bars(self, symbol, interval, count, priority=None):
            self.call_count += 1
            return super().get_raw_bars(symbol, interval, count)

    broker = _CountingRawBarsBroker({"ONE": [_bar(5, 6, 100)]}, prices={"ONE": 5.0})
    scanner = BroadScanner(broker, _FakeFloatProvider())

    scanner.scan(["ONE"])

    assert broker.call_count == 1


# -- volume_baseline (also shares the same raw bars -- see
# metrics/volume_baseline.py and scanner/candidate_watcher.py's RVOL lookup) --

def test_scan_populates_volume_baseline_from_raw_bars():
    # 2026-08-03/04 are EDT (UTC-4) -- 9:30am ET == 13:30 UTC, RTH bucket 0.
    # "Today" (now_fn) is 2026-08-05, so both are historical days the
    # baseline should average over.
    bars = [
        _bar(5, 6, 100, time="2026-08-03T13:30:00.000+0000"),
        _bar(5, 6, 300, time="2026-08-04T13:30:00.000+0000"),
    ]
    broker = _RawBarsAwareBroker({"BASE": bars}, prices={"BASE": 5.0})
    scanner = BroadScanner(broker, _FakeFloatProvider(), now_fn=lambda: datetime(2026, 8, 5, 15, 0, 0))

    candidates = scanner.scan(["BASE"])

    assert candidates[0].volume_baseline is not None
    typical_same_time, _, typical_5m = candidates[0].volume_baseline.lookup(datetime(2026, 8, 5, 13, 30))
    assert typical_same_time == 200.0
    assert typical_5m == 200.0


def test_scan_leaves_volume_baseline_none_without_get_raw_bars():
    broker = _SlowFakeBroker(delay_seconds=0.0, prices={"ANY": 5.0})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    candidates = scanner.scan(["ANY"])
    assert candidates[0].volume_baseline is None


def test_scan_leaves_volume_baseline_none_on_raw_bars_failure():
    class _FlakyRawBarsBroker(_RawBarsAwareBroker):
        def get_raw_bars(self, symbol, interval, count, priority=None):
            raise RuntimeError("simulated Webull failure")

    broker = _FlakyRawBarsBroker({}, prices={"ANY": 5.0})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    candidates = scanner.scan(["ANY"])
    assert len(candidates) == 1
    assert candidates[0].volume_baseline is None


# -- seed_snapshots (also shares the same raw bars -- see
# metrics/rolling.seed_history_from_bars and CandidateWatcher._push_history) --

class _FixedTimestampBroker(_RawBarsAwareBroker):
    """get_snapshot() normally stamps datetime.utcnow() (see _snapshot()
    above), which seed_snapshots tests can't use deterministically -- this
    pins the discovery snapshot to a controlled timestamp instead."""

    def __init__(self, raw_bars, prices, snapshot_time):
        super().__init__(raw_bars, prices)
        self.snapshot_time = snapshot_time

    def get_snapshot(self, symbol):
        price = self.prices.get(symbol, 5.0)
        return MarketSnapshot(
            symbol=symbol, timestamp=self.snapshot_time, last_price=price, bid=price - 0.01,
            ask=price + 0.01, bid_size=100, ask_size=100, cumulative_volume=1_000_000.0,
            vwap=price, high_of_day=price, low_of_day=price, open_price=price,
        )


def test_scan_populates_seed_snapshots_from_raw_bars():
    discovery_time = datetime(2026, 8, 10, 13, 30, 0)
    bars = [
        _bar(9, 10, 400_000, time="2026-08-10T13:26:00.000+0000", close=10.0),
        _bar(9, 10, 600_000, time="2026-08-10T13:28:00.000+0000", close=10.0),
    ]
    broker = _FixedTimestampBroker({"SEED": bars}, prices={"SEED": 10.0}, snapshot_time=discovery_time)
    scanner = BroadScanner(broker, _FakeFloatProvider())

    candidates = scanner.scan(["SEED"])

    seeded = candidates[0].seed_snapshots
    assert len(seeded) == 2
    assert seeded[-1].cumulative_volume == 1_000_000.0  # anchored to the discovery snapshot's real total


def test_scan_leaves_seed_snapshots_empty_without_get_raw_bars():
    broker = _SlowFakeBroker(delay_seconds=0.0, prices={"ANY": 5.0})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    candidates = scanner.scan(["ANY"])
    assert candidates[0].seed_snapshots == []


def test_scan_leaves_seed_snapshots_empty_on_raw_bars_failure():
    class _FlakyRawBarsBroker(_RawBarsAwareBroker):
        def get_raw_bars(self, symbol, interval, count, priority=None):
            raise RuntimeError("simulated Webull failure")

    broker = _FlakyRawBarsBroker({}, prices={"ANY": 5.0})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    candidates = scanner.scan(["ANY"])
    assert len(candidates) == 1
    assert candidates[0].seed_snapshots == []


# -- resistance refresh on rescans (periodic re-computation for already-
# tracked, pre-entry candidates -- see TradingLoop._refresh_stale_resistance_levels) --

def test_scan_sets_resistance_last_refreshed_at_at_discovery():
    broker = _RawBarsAwareBroker({"CLUSTERED": [_bar(8, 9, 5000)]}, prices={"CLUSTERED": 5.0})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    candidates = scanner.scan(["CLUSTERED"])
    assert candidates[0].resistance_last_refreshed_at is not None


def test_refresh_resistance_levels_recomputes_from_fresh_bars():
    bars = [_bar(5, 10, 100), _bar(8, 9, 5000), _bar(8, 9, 5000)]
    broker = _RawBarsAwareBroker({"CLUSTERED": bars}, prices={"CLUSTERED": 5.0})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    candidate = scanner.scan(["CLUSTERED"])[0]

    # Simulate the volume profile shifting since discovery: a new, tighter
    # cluster around 11-12 now dominates.
    broker.raw_bars["CLUSTERED"] = [_bar(5, 10, 100), _bar(11, 12, 9000), _bar(11, 12, 9000)]
    refresh_time = datetime(2026, 8, 10, 15, 0, 0)

    scanner.refresh_resistance_levels(candidate, now=refresh_time)

    assert any(11 <= level <= 12 for level in candidate.static_resistance_levels)
    assert candidate.resistance_last_refreshed_at == refresh_time


def test_refresh_resistance_levels_leaves_state_unchanged_on_fetch_failure():
    class _FlakyRawBarsBroker(_RawBarsAwareBroker):
        def get_raw_bars(self, symbol, interval, count, priority=None):
            raise RuntimeError("simulated Webull failure")

    broker = _FlakyRawBarsBroker({}, prices={"ANY": 5.0})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    candidate = scanner.scan(["ANY"])[0]
    original_levels = candidate.static_resistance_levels
    original_refreshed_at = candidate.resistance_last_refreshed_at

    scanner.refresh_resistance_levels(candidate, now=datetime(2026, 8, 10, 15, 0, 0))

    # A failed refresh must not clobber previously-good levels or reset the
    # throttle clock -- see refresh_resistance_levels' docstring.
    assert candidate.static_resistance_levels == original_levels
    assert candidate.resistance_last_refreshed_at == original_refreshed_at


def test_refresh_resistance_levels_does_not_touch_opening_range_high():
    bars = [_bar(5, 6.5, 1000, time="2026-08-03T13:32:00.000+0000")]
    broker = _RawBarsAwareBroker({"OPEN": bars}, prices={"OPEN": 5.0})
    scanner = BroadScanner(broker, _FakeFloatProvider(), now_fn=lambda: datetime(2026, 8, 3, 15, 0, 0))
    candidate = scanner.scan(["OPEN"])[0]
    original_opening_range_high = candidate.opening_range_high

    broker.raw_bars["OPEN"] = [_bar(5, 20, 1000, time="2026-08-03T13:32:00.000+0000")]  # would change it if touched
    scanner.refresh_resistance_levels(candidate, now=datetime(2026, 8, 3, 16, 0, 0))

    assert candidate.opening_range_high == original_opening_range_high


# -- check_symbol_verbose (used by the dashboard's on-demand single-ticker
# scan, dashboard/app.py's POST /api/scan-symbol) -- same structural checks
# as _check_symbol/scan(), but also explains a rejection instead of
# silently returning None. ------------------------------------------------

def test_check_symbol_verbose_returns_candidate_and_no_reason_on_success():
    broker = _SlowFakeBroker(delay_seconds=0.0, prices={"GOOD": 5.0})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    candidate, reason = scanner.check_symbol_verbose("GOOD")
    assert candidate is not None
    assert candidate.symbol == "GOOD"
    assert candidate.state == CandidateState.WATCHING
    assert reason is None


def test_check_symbol_verbose_explains_price_rejection():
    broker = _SlowFakeBroker(delay_seconds=0.0, prices={"EXPENSIVE": 30.0})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    candidate, reason = scanner.check_symbol_verbose("EXPENSIVE")
    assert candidate is None
    assert "30.00" in reason
    assert "range" in reason.lower()


def test_check_symbol_verbose_explains_float_rejection():
    broker = _SlowFakeBroker(delay_seconds=0.0, prices={"BIGFLOAT": 5.0})
    float_provider = _FakeFloatProvider({"BIGFLOAT": 50_000_000})
    scanner = BroadScanner(broker, float_provider)
    candidate, reason = scanner.check_symbol_verbose("BIGFLOAT")
    assert candidate is None
    assert "50,000,000" in reason
    assert "float" in reason.lower()


def test_check_symbol_verbose_explains_volume_floor_rejection():
    broker = _DailyVolumeAwareBroker({"DEAD": [400_000] * 10})  # current-day defaults to 200,000
    scanner = BroadScanner(broker, _FakeFloatProvider())
    candidate, reason = scanner.check_symbol_verbose("DEAD")
    assert candidate is None
    assert "volume" in reason.lower()
    assert "400,000" in reason  # the actual average/previous-day figure


def test_check_symbol_verbose_explains_broker_failure():
    class _FlakyBroker(_SlowFakeBroker):
        def get_snapshot(self, symbol):
            raise RuntimeError("simulated broker failure")

    scanner = BroadScanner(_FlakyBroker(0.0, {}), _FakeFloatProvider())
    candidate, reason = scanner.check_symbol_verbose("BROKEN")
    assert candidate is None
    assert "BROKEN" in reason
    assert "simulated broker failure" in reason
