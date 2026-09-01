from __future__ import annotations

from .rule_based_strategy import RuleBasedStrategy
from .schema import StrategyConfig


def compile(config: StrategyConfig) -> RuleBasedStrategy:
    return RuleBasedStrategy(config)
