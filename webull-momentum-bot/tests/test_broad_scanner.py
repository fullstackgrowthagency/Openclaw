"""
Tests for BroadScanner, including its concurrent per-symbol checking
(scanner/broad_scanner.py). Uses fakes with an artificial delay to prove
the thread pool actually runs checks in parallel, not just that results are
correct -- correctness alone wouldn't catch a regression back to sequential
scanning.
"""
import time
from datetime import datetime

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


def test_scan_filters_price_and_free_float():
    symbols = ["CHEAP", "GOOD", "EXPENSIVE", "BIGFLOAT"]
    broker = _SlowFakeBroker(delay_seconds=0.0, prices={"CHEAP": 0.50, "GOOD": 5.0, "EXPENSIVE": 25.0, "BIGFLOAT": 5.0})
    float_provider = _FakeFloatProvider({"BIGFLOAT": 50_000_000})
    scanner = BroadScanner(broker, float_provider)

    candidates = scanner.scan(symbols)
    assert [c.symbol for c in candidates] == ["GOOD"]
    assert candidates[0].state == CandidateState.WATCHING


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


def test_scan_min_dollar_volume_filter():
    class _VolumeAwareBroker(_SlowFakeBroker):
        def get_snapshot(self, symbol):
            volume = 1_000 if symbol == "LOWVOL" else 200_000
            return _snapshot(symbol, price=5.0, cumulative_volume=volume)

    scanner = BroadScanner(_VolumeAwareBroker(0.0, {}), _FakeFloatProvider(), BroadScannerConfig(min_dollar_volume=200_000))
    candidates = scanner.scan(["LOWVOL", "HIGHVOL"])
    assert [c.symbol for c in candidates] == ["HIGHVOL"]


class _DailyVolumeAwareBroker(_SlowFakeBroker):
    """Adds get_daily_volumes -- a capability real WebullBrokerClient has
    but _SlowFakeBroker (standing in for paper/backtest) does not, since
    _passes_average_volume_filter is meant to be a no-op without it."""

    def __init__(self, daily_volumes: dict[str, list[float]]):
        super().__init__(delay_seconds=0.0, prices={})
        self.daily_volumes = daily_volumes

    def get_daily_volumes(self, symbol, lookback_days):
        return self.daily_volumes[symbol]


def test_scan_rejects_low_average_volume_symbol():
    broker = _DailyVolumeAwareBroker({"QUIET": [500_000] * 10})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    assert scanner.scan(["QUIET"]) == []


def test_scan_keeps_low_average_volume_symbol_with_a_big_previous_day():
    # Average is well under the 1M bar, but the most recent (index 0,
    # most-recent-first) day alone cleared it -- the "quiet float just woke
    # up" exception the filter exists for.
    broker = _DailyVolumeAwareBroker({"WOKEUP": [1_500_000] + [200_000] * 9})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    candidates = scanner.scan(["WOKEUP"])
    assert [c.symbol for c in candidates] == ["WOKEUP"]


def test_scan_keeps_symbol_with_healthy_average_volume():
    broker = _DailyVolumeAwareBroker({"LIQUID": [2_000_000] * 10})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    candidates = scanner.scan(["LIQUID"])
    assert [c.symbol for c in candidates] == ["LIQUID"]


def test_scan_rejects_symbol_on_daily_volume_lookup_failure():
    class _FlakyDailyVolumeBroker(_DailyVolumeAwareBroker):
        def get_daily_volumes(self, symbol, lookback_days):
            raise RuntimeError("simulated Webull failure")

    broker = _FlakyDailyVolumeBroker({})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    assert scanner.scan(["ANY"]) == []


def test_scan_average_volume_filter_is_a_no_op_without_get_daily_volumes():
    # _SlowFakeBroker has no get_daily_volumes at all -- representative of
    # PaperBrokerClient, which has no real daily-volume history to check
    # against. The filter must not reject anything in that case.
    broker = _SlowFakeBroker(delay_seconds=0.0, prices={"ANY": 5.0})
    scanner = BroadScanner(broker, _FakeFloatProvider())
    candidates = scanner.scan(["ANY"])
    assert [c.symbol for c in candidates] == ["ANY"]


class _RawBarsAwareBroker(_SlowFakeBroker):
    """Adds get_raw_bars -- a capability real WebullBrokerClient has but
    _SlowFakeBroker (standing in for paper/backtest) does not, since
    _compute_static_resistance_levels is meant to be a no-op without it."""

    def __init__(self, raw_bars: dict[str, list[dict]], prices: dict[str, float] = None):
        super().__init__(delay_seconds=0.0, prices=prices or {})
        self.raw_bars = raw_bars

    def get_raw_bars(self, symbol, interval, count):
        return self.raw_bars[symbol]


def _bar(low, high, volume, time="2026-08-01T12:00:00.000+0000"):
    return {"time": time, "low": str(low), "high": str(high), "volume": str(volume)}


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
        def get_raw_bars(self, symbol, interval, count):
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
