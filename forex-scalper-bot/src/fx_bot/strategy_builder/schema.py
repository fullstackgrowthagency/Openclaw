"""
The declarative strategy-config schema -- the ONE schema authored by both
a human via a future form UI and by the AI authoring assistant's
structured proposals (see the approved plan's rule-builder design). A
validated StrategyConfig compiles into a RuleBasedStrategy implementing
the Strategy ABC (see compiler.py) -- nothing downstream (TriggerEngine-
equivalent, RiskEngine, BacktestEngine) needs to know or care whether a
config was hand-authored, AI-authored, or came from a template.

Trimmed to exactly what's real right now, not the full field set the
approved plan eventually calls for:
- No `timeframe` field -- meaningless without true OHLC bar aggregation,
  which doesn't exist yet (see indicators/registry.py's docstring).
- No `position_sizing` section -- RiskEngine.evaluate() decides
  max_units today (see risk/risk_engine.py); a config-driven sizing
  override is a Phase 4 concern once RiskEngine actually reads per-
  config parameters.
- No `filters` section (max_spread_pips, session_windows, max_concurrent_
  positions_this_pair) -- these are RiskEngine-level concerns per the
  approved plan's own field-mapping table, not evaluated by anything yet.
- stop_loss/take_profit only support `fixed_pips` (take_profit also
  supports `risk_reward_ratio`) -- `atr_multiple` needs the ATR indicator,
  which needs true OHLC bars (not built yet, see indicators/registry.py).

Add each of the above back once the infrastructure it depends on exists,
rather than shipping schema fields nothing evaluates.
"""
from __future__ import annotations

from typing import Literal, Union

from pydantic import BaseModel, Field, field_validator, model_validator

from ..pairs import base_quote


class PriceRef(BaseModel):
    kind: Literal["price"] = "price"
    field: Literal["mid", "bid", "ask"] = "mid"


class ConstantRef(BaseModel):
    kind: Literal["constant"] = "constant"
    value: float


class IndicatorValueRef(BaseModel):
    kind: Literal["indicator"] = "indicator"
    indicator_id: str


Operand = Union[PriceRef, ConstantRef, IndicatorValueRef]


class Condition(BaseModel):
    left: Operand = Field(discriminator="kind")
    operator: Literal["gt", "lt", "gte", "lte", "eq", "crosses_above", "crosses_below"]
    right: Operand = Field(discriminator="kind")


class ConditionGroup(BaseModel):
    op: Literal["and", "or", "not"]
    items: list[Union[Condition, "ConditionGroup"]]

    @field_validator("items")
    @classmethod
    def _not_takes_exactly_one_item(cls, items, info):
        if info.data.get("op") == "not" and len(items) != 1:
            raise ValueError("A 'not' group must have exactly one item.")
        if not items:
            raise ValueError("A condition group must have at least one item.")
        return items


ConditionGroup.model_rebuild()


class IndicatorRef(BaseModel):
    id: str
    type: str
    params: dict[str, Union[int, float]] = Field(default_factory=dict)


class StopRule(BaseModel):
    type: Literal["fixed_pips"] = "fixed_pips"
    pips: float = Field(gt=0)


class TargetRule(BaseModel):
    type: Literal["fixed_pips", "risk_reward_ratio"]
    pips: float | None = Field(default=None, gt=0)
    ratio: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _matching_field_is_set(self) -> "TargetRule":
        if self.type == "fixed_pips" and self.pips is None:
            raise ValueError("take_profit.type == 'fixed_pips' requires 'pips'.")
        if self.type == "risk_reward_ratio" and self.ratio is None:
            raise ValueError("take_profit.type == 'risk_reward_ratio' requires 'ratio'.")
        return self


class StrategyConfig(BaseModel):
    id: str
    name: str
    version: int = 1
    pair: str
    indicators: list[IndicatorRef] = Field(default_factory=list)
    entry_side: Literal["long", "short"]
    entry_conditions: ConditionGroup
    # Optional rule-based exit IN ADDITION to stop_loss/take_profit --
    # None means the position is only ever closed by the stop/target
    # levels computed at entry (position management, a later phase,
    # actually enforces those; nothing does yet -- see rule_based_
    # strategy.py's docstring).
    exit_conditions: ConditionGroup | None = None
    stop_loss: StopRule
    take_profit: TargetRule

    @field_validator("pair")
    @classmethod
    def _pair_is_well_formed(cls, pair: str) -> str:
        base_quote(pair)  # raises ValueError on anything not "BASE/QUOTE"
        return pair
