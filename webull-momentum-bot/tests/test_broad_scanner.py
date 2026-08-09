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
