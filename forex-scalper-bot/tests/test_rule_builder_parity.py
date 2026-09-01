"""
Phase 3's core proof: a StrategyConfig compiled via strategy_builder must
produce the SAME trading behavior in backtest as an equivalent hand-coded
Strategy subclass. If a config-driven strategy behaved differently from
its hand-coded equivalent, the whole point of the rule-builder (letting
users -- or the AI assistant -- define a strategy declaratively instead
of writing code) would be broken: results wouldn't be trustworthy.
"""
from datetime import datetime

import pytest

from fx_bot.backtest.engine import BacktestEngine
from fx_bot.brokers.paper.client import PaperBrokerClient
from fx_bot.enums import SignalAction
from fx_bot.execution.order_manager import OrderManager
from fx_bot.indicators.moving_average import ema
from fx_bot.interfaces.strategy import Strategy
from fx_bot.models import MarketSnapshot, Signal
from fx_bot.pairs import pips_to_price_diff
from fx_bot.risk.risk_engine import RiskEngine
from fx_bot.strategy_builder.compiler import compile
from fx_bot.strategy_builder.schema import StrategyConfig

_FAST_PERIOD = 2
_SLOW_PERIOD = 4
_STOP_PIPS = 10
_TARGET_PIPS = 20
_PAIR = "EUR/USD"


class _HandCodedEmaCrossoverStrategy(Strategy):
    """The same EMA(2)/EMA(4) crossover entry+exit logic as the
    StrategyConfig built below, written directly against the Strategy
    ABC -- the pre-rule-builder way of authoring a strategy."""
    name = "hand_coded_ema_cross"
    version = "v1"

    def on_snapshot(self, symbol, snapshot, history, position):
        if symbol != _PAIR:
            return None
        prices = [bar.mid for bar in history]
        fast = ema(prices, _FAST_PERIOD)
        slow = ema(prices, _SLOW_PERIOD)
        if len(fast) < 2 or None in (fast[-1], fast[-2], slow[-1], slow[-2]):
            return None
        crossed_above = fast[-2] <= slow[-2] and fast[-1] > slow[-1]
        crossed_below = fast[-2] >= slow[-2] and fast[-1] < slow[-1]

        if position is None:
            if not crossed_above:
                return None
            stop_distance = pips_to_price_diff(symbol, _STOP_PIPS)
            target_distance = pips_to_price_diff(symbol, _TARGET_PIPS)
            return Signal(
                symbol=symbol, action=SignalAction.ENTER_LONG, generated_at=snapshot.timestamp,
                strategy_name=self.name, strategy_version=self.version, reference_price=snapshot.mid,
                suggested_stop=snapshot.mid - stop_distance, suggested_target=snapshot.mid + target_distance,
            )
        if crossed_below:
            return Signal(
                symbol=symbol, action=SignalAction.EXIT, generated_at=snapshot.timestamp,
                strategy_name=self.name, strategy_version=self.version, reference_price=snapshot.mid,
            )
        return None


def _equivalent_config() -> StrategyConfig:
    return StrategyConfig(
        id="ema-cross-1", name="rule_based_ema_cross", pair=_PAIR, entry_side="long",
        indicators=[
            {"id": "fast", "type": "ema", "params": {"period": _FAST_PERIOD}},
            {"id": "slow", "type": "ema", "params": {"period": _SLOW_PERIOD}},
        ],
        entry_conditions={
            "op": "and",
            "items": [{
                "left": {"kind": "indicator", "indicator_id": "fast"},
                "operator": "crosses_above",
                "right": {"kind": "indicator", "indicator_id": "slow"},
            }],
        },
        exit_conditions={
            "op": "and",
            "items": [{
                "left": {"kind": "indicator", "indicator_id": "fast"},
                "operator": "crosses_below",
                "right": {"kind": "indicator", "indicator_id": "slow"},
            }],
        },
        stop_loss={"pips": _STOP_PIPS},
        take_profit={"type": "fixed_pips", "pips": _TARGET_PIPS},
    )


def _bars() -> list[MarketSnapshot]:
    # Falling, then a sustained rise (triggers crosses_above), then a
    # sustained fall again (triggers crosses_below) -- one full entry+exit
    # round trip for either strategy.
    mids = [
        1.1050, 1.1040, 1.1030, 1.1020, 1.1010,
        1.1050, 1.1090, 1.1130, 1.1170, 1.1210,
        1.1170, 1.1130, 1.1090, 1.1050, 1.1010,
    ]
    return [
        MarketSnapshot(symbol=_PAIR, timestamp=datetime(2026, 1, 1, 0, i, 0), bid=m - 0.0001, ask=m + 0.0001)
        for i, m in enumerate(mids)
    ]


def _run(strategy: Strategy):
    broker = PaperBrokerClient()
    order_manager = OrderManager(broker, RiskEngine())
    engine = BacktestEngine(strategy, broker, order_manager)
    return engine.run(_bars())


def test_compiled_config_and_hand_coded_strategy_produce_the_same_trade():
    hand_coded_trades = _run(_HandCodedEmaCrossoverStrategy())
    rule_based_trades = _run(compile(_equivalent_config()))

    assert len(hand_coded_trades) == 1
    assert len(rule_based_trades) == 1

    hand_coded, rule_based = hand_coded_trades[0], rule_based_trades[0]
    assert hand_coded.symbol == rule_based.symbol
    assert hand_coded.side == rule_based.side
    assert hand_coded.entry_price == pytest.approx(rule_based.entry_price)
    assert hand_coded.exit_price == pytest.approx(rule_based.exit_price)
    assert hand_coded.quantity == pytest.approx(rule_based.quantity)
    assert hand_coded.pnl == pytest.approx(rule_based.pnl)
    assert hand_coded.opened_at == rule_based.opened_at
    assert hand_coded.closed_at == rule_based.closed_at
