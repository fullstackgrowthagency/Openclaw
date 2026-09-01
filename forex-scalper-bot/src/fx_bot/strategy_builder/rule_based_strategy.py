"""
RuleBasedStrategy -- the ONE Strategy implementation every validated
StrategyConfig compiles into (see compiler.py). Whether a config was
hand-authored, came from a built-in template, or was produced by the AI
authoring assistant, it becomes exactly this class at runtime -- nothing
downstream needs a separate code path per authoring method.

Indicator series are recomputed fresh from the full `history` on every
call rather than maintained incrementally -- simple and correct, if not
maximally efficient; revisit only if profiling ever shows this matters
(scalping's short lookback windows keep `history` small in practice).
"""
from __future__ import annotations

from typing import Optional, Union

from ..enums import SignalAction
from ..indicators.registry import INDICATOR_REGISTRY
from ..interfaces.strategy import Strategy
from ..models import MarketSnapshot, Position, Signal
from ..pairs import pips_to_price_diff
from .schema import Condition, ConditionGroup, ConstantRef, IndicatorValueRef, PriceRef, StrategyConfig

Operand = Union[PriceRef, ConstantRef, IndicatorValueRef]


class RuleBasedStrategy(Strategy):
    def __init__(self, config: StrategyConfig):
        self.config = config
        self.name = config.name
        self.version = str(config.version)

    def on_snapshot(
        self, symbol: str, snapshot: MarketSnapshot, history: list[MarketSnapshot], position: Optional[Position],
    ) -> Optional[Signal]:
        if symbol != self.config.pair:
            return None

        prices = [bar.mid for bar in history]
        indicator_series = {
            ind.id: INDICATOR_REGISTRY[ind.type]["fn"](prices, **ind.params)
            for ind in self.config.indicators
        }

        if position is not None:
            if self.config.exit_conditions is not None and self._evaluate_group(
                self.config.exit_conditions, history, indicator_series,
            ):
                return Signal(
                    symbol=symbol, action=SignalAction.EXIT, generated_at=snapshot.timestamp,
                    strategy_name=self.name, strategy_version=self.version, reference_price=snapshot.mid,
                )
            return None

        if not self._evaluate_group(self.config.entry_conditions, history, indicator_series):
            return None

        stop, target = self._compute_stop_and_target(snapshot.mid)
        action = SignalAction.ENTER_LONG if self.config.entry_side == "long" else SignalAction.ENTER_SHORT
        return Signal(
            symbol=symbol, action=action, generated_at=snapshot.timestamp,
            strategy_name=self.name, strategy_version=self.version, reference_price=snapshot.mid,
            suggested_stop=stop, suggested_target=target,
        )

    def _compute_stop_and_target(self, entry_price: float) -> tuple[float, float]:
        pair = self.config.pair
        is_long = self.config.entry_side == "long"
        stop_distance = pips_to_price_diff(pair, self.config.stop_loss.pips)
        stop = entry_price - stop_distance if is_long else entry_price + stop_distance

        take_profit = self.config.take_profit
        if take_profit.type == "fixed_pips":
            target_distance = pips_to_price_diff(pair, take_profit.pips)
        else:
            target_distance = stop_distance * take_profit.ratio
        target = entry_price + target_distance if is_long else entry_price - target_distance

        return stop, target

    def _evaluate_group(
        self, group: ConditionGroup, history: list[MarketSnapshot], indicator_series: dict,
    ) -> bool:
        def evaluate_item(item):
            if isinstance(item, ConditionGroup):
                return self._evaluate_group(item, history, indicator_series)
            return self._evaluate_condition(item, history, indicator_series)

        if group.op == "and":
            return all(evaluate_item(item) for item in group.items)
        if group.op == "or":
            return any(evaluate_item(item) for item in group.items)
        return not evaluate_item(group.items[0])  # "not" -- schema guarantees exactly one item

    def _evaluate_condition(
        self, condition: Condition, history: list[MarketSnapshot], indicator_series: dict,
    ) -> bool:
        current_left = self._resolve(condition.left, history, indicator_series, offset=0)
        current_right = self._resolve(condition.right, history, indicator_series, offset=0)
        if current_left is None or current_right is None:
            return False  # not enough history yet -- a safe "not triggered", never an error

        if condition.operator == "gt":
            return current_left > current_right
        if condition.operator == "lt":
            return current_left < current_right
        if condition.operator == "gte":
            return current_left >= current_right
        if condition.operator == "lte":
            return current_left <= current_right
        if condition.operator == "eq":
            return current_left == current_right

        previous_left = self._resolve(condition.left, history, indicator_series, offset=1)
        previous_right = self._resolve(condition.right, history, indicator_series, offset=1)
        if previous_left is None or previous_right is None:
            return False
        if condition.operator == "crosses_above":
            return previous_left <= previous_right and current_left > current_right
        return previous_left >= previous_right and current_left < current_right  # crosses_below

    @staticmethod
    def _resolve(
        operand: Operand, history: list[MarketSnapshot], indicator_series: dict, offset: int,
    ) -> Optional[float]:
        if isinstance(operand, ConstantRef):
            return operand.value
        if isinstance(operand, PriceRef):
            index = len(history) - 1 - offset
            if index < 0:
                return None
            return getattr(history[index], operand.field)
        series = indicator_series[operand.indicator_id]
        index = len(series) - 1 - offset
        if index < 0:
            return None
        return series[index]
