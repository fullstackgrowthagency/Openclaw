from datetime import datetime

import pytest

from fx_bot.backtest.engine import BacktestEngine
from fx_bot.brokers.paper.client import PaperBrokerClient
from fx_bot.enums import ExitReason, SignalAction
from fx_bot.execution.order_manager import OrderManager
from fx_bot.interfaces.strategy import Strategy
from fx_bot.models import MarketSnapshot, Signal
from fx_bot.position.position_manager import PositionManagementConfig, PositionManager
from fx_bot.risk.risk_engine import RiskEngine


class _EntryThenExitStrategy(Strategy):
    """Canned, deterministic strategy: enters long on the first snapshot
    it ever sees, exits on the third -- just enough to prove a full
    Strategy -> RiskEngine -> OrderManager -> PaperBrokerClient round
    trip produces a real Trade, not a test of any real trading logic."""
    name = "entry_then_exit"
    version = "v1"

    def __init__(self):
        self.calls = 0

    def on_snapshot(self, symbol, snapshot, history, position):
        self.calls += 1
        if self.calls == 1:
            return Signal(
                symbol=symbol, action=SignalAction.ENTER_LONG, generated_at=snapshot.timestamp,
                strategy_name=self.name, strategy_version=self.version, reference_price=snapshot.mid,
                suggested_stop=snapshot.mid - 0.0050,
            )
        if self.calls == 3:
            return Signal(
                symbol=symbol, action=SignalAction.EXIT, generated_at=snapshot.timestamp,
                strategy_name=self.name, strategy_version=self.version, reference_price=snapshot.mid,
            )
        return None


def _rising_bars(count: int) -> list[MarketSnapshot]:
    return [
        MarketSnapshot(
            symbol="EUR/USD", timestamp=datetime(2026, 1, 1, 0, i, 0),
            bid=1.1000 + i * 0.0010, ask=1.1002 + i * 0.0010,
        )
        for i in range(count)
    ]


def test_backtest_runs_a_full_entry_to_exit_round_trip():
    strategy = _EntryThenExitStrategy()
    broker = PaperBrokerClient()
    order_manager = OrderManager(broker, RiskEngine())
    engine = BacktestEngine(strategy, broker, order_manager)

    trades = engine.run(_rising_bars(4))

    assert len(trades) == 1
    trade = trades[0]
    assert trade.symbol == "EUR/USD"
    assert trade.pnl > 0  # price rose the whole time this strategy was long
    assert broker.get_positions() == []  # closed, nothing left open


def test_backtest_sorts_out_of_order_bars_before_processing():
    strategy = _EntryThenExitStrategy()
    broker = PaperBrokerClient()
    order_manager = OrderManager(broker, RiskEngine())
    engine = BacktestEngine(strategy, broker, order_manager)

    bars = _rising_bars(4)
    shuffled = [bars[2], bars[0], bars[3], bars[1]]  # deliberately out of chronological order

    trades = engine.run(shuffled)

    assert len(trades) == 1  # same result as the properly-ordered case
    assert trades[0].pnl > 0


class _EntersOnceAndNeverExitsStrategy(Strategy):
    """Enters long on the first snapshot with a fixed stop, and NEVER
    itself emits an EXIT signal -- any close in these tests must come
    from PositionManager's own auto-enforcement, not the strategy."""
    name = "enters_once"
    version = "v1"

    def __init__(self):
        self.calls = 0
        self.positions_seen: list[object] = []

    def on_snapshot(self, symbol, snapshot, history, position):
        self.calls += 1
        self.positions_seen.append(position)
        if self.calls == 1:
            return Signal(
                symbol=symbol, action=SignalAction.ENTER_LONG, generated_at=snapshot.timestamp,
                strategy_name=self.name, strategy_version=self.version, reference_price=snapshot.mid,
                suggested_stop=snapshot.mid - 0.0010,
            )
        return None


def test_position_manager_auto_closes_on_stop_loss_without_the_strategy_emitting_an_exit():
    strategy = _EntersOnceAndNeverExitsStrategy()
    broker = PaperBrokerClient()
    order_manager = OrderManager(broker, RiskEngine())
    engine = BacktestEngine(strategy, broker, order_manager)

    bars = [
        MarketSnapshot(symbol="EUR/USD", timestamp=datetime(2026, 1, 1, 0, 0, 0), bid=1.0999, ask=1.1001),
        MarketSnapshot(symbol="EUR/USD", timestamp=datetime(2026, 1, 1, 0, 1, 0), bid=1.1004, ask=1.1006),
        # Falls through the 1.1001 - 0.0010 = 1.0991 stop.
        MarketSnapshot(symbol="EUR/USD", timestamp=datetime(2026, 1, 1, 0, 2, 0), bid=1.0985, ask=1.0987),
    ]

    trades = engine.run(bars)

    assert len(trades) == 1
    assert trades[0].exit_reason == ExitReason.STOP_LOSS
    assert broker.get_positions() == []
    # The strategy itself never returned an EXIT signal (see the class
    # above) -- the close came entirely from PositionManager.
    assert strategy.calls == 3


def test_strategy_sees_no_position_the_same_tick_position_manager_closes_it():
    strategy = _EntersOnceAndNeverExitsStrategy()
    broker = PaperBrokerClient()
    order_manager = OrderManager(broker, RiskEngine())
    engine = BacktestEngine(strategy, broker, order_manager)

    bars = [
        MarketSnapshot(symbol="EUR/USD", timestamp=datetime(2026, 1, 1, 0, 0, 0), bid=1.0999, ask=1.1001),
        MarketSnapshot(symbol="EUR/USD", timestamp=datetime(2026, 1, 1, 0, 1, 0), bid=1.0985, ask=1.0987),  # stop hit
    ]

    engine.run(bars)

    assert strategy.positions_seen[0] is None       # before any entry
    assert strategy.positions_seen[1] is None        # already auto-closed by the time the strategy is asked


def test_position_manager_trailing_stop_and_breakeven_are_configurable_on_the_backtest_engine():
    strategy = _EntersOnceAndNeverExitsStrategy()
    broker = PaperBrokerClient()
    order_manager = OrderManager(broker, RiskEngine())
    position_manager = PositionManager(PositionManagementConfig(trailing_stop_pips=10))
    engine = BacktestEngine(strategy, broker, order_manager, position_manager=position_manager)

    bars = [
        MarketSnapshot(symbol="EUR/USD", timestamp=datetime(2026, 1, 1, 0, 0, 0), bid=1.0999, ask=1.1001),
        MarketSnapshot(symbol="EUR/USD", timestamp=datetime(2026, 1, 1, 0, 1, 0), bid=1.1029, ask=1.1031),  # trails to ~1.1019
        MarketSnapshot(symbol="EUR/USD", timestamp=datetime(2026, 1, 1, 0, 2, 0), bid=1.1015, ask=1.1017),  # pulls back through it
    ]

    trades = engine.run(bars)

    assert len(trades) == 1
    assert trades[0].exit_reason == ExitReason.STOP_LOSS
    assert trades[0].pnl > 0  # trailing locked in a profit above the original fixed stop


def test_backtest_produces_no_trade_when_the_entry_never_fires():
    class _NeverEntersStrategy(Strategy):
        name = "never_enters"
        version = "v1"
        def on_snapshot(self, symbol, snapshot, history, position):
            return None

    broker = PaperBrokerClient()
    order_manager = OrderManager(broker, RiskEngine())
    engine = BacktestEngine(_NeverEntersStrategy(), broker, order_manager)

    trades = engine.run(_rising_bars(4))

    assert trades == []
