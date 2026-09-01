"""
validate_strategy_config is the ONE function every StrategyConfig must
pass through before it's ever compiled or shown to a user as "ready" --
see the approved plan's AI-authoring-assistant design, which feeds this
function's errors back to Claude for a repair attempt. Wraps both
Pydantic's own structural validation and the semantic checks below
(indicator references, registry membership) into a single
StrategyConfigError with a flat list of human-readable messages, so a
caller never needs to handle two different exception shapes depending on
what kind of problem was found.
"""
from __future__ import annotations

from pydantic import ValidationError

from ..indicators.registry import INDICATOR_REGISTRY
from .schema import Condition, ConditionGroup, IndicatorValueRef, StrategyConfig


class StrategyConfigError(Exception):
    def __init__(self, messages: list[str]):
        self.messages = messages
        super().__init__("; ".join(messages))


def validate_strategy_config(data: dict) -> StrategyConfig:
    try:
        config = StrategyConfig.model_validate(data)
    except ValidationError as exc:
        raise StrategyConfigError([_format_pydantic_error(e) for e in exc.errors()]) from exc

    errors = _semantic_errors(config)
    if errors:
        raise StrategyConfigError(errors)
    return config


def _format_pydantic_error(error: dict) -> str:
    path = ".".join(str(p) for p in error["loc"]) or "(root)"
    return f"{path}: {error['msg']}"


def _semantic_errors(config: StrategyConfig) -> list[str]:
    errors: list[str] = []
    declared_ids = {ind.id for ind in config.indicators}

    for indicator in config.indicators:
        spec = INDICATOR_REGISTRY.get(indicator.type)
        if spec is None:
            errors.append(f"indicators[{indicator.id!r}]: unknown indicator type {indicator.type!r}.")
            continue
        missing = set(spec["params"]) - set(indicator.params)
        if missing:
            errors.append(
                f"indicators[{indicator.id!r}]: missing required param(s) {sorted(missing)} for type {indicator.type!r}."
            )

    referenced_ids = set()
    if config.entry_conditions is not None:
        referenced_ids |= _referenced_indicator_ids(config.entry_conditions)
    if config.exit_conditions is not None:
        referenced_ids |= _referenced_indicator_ids(config.exit_conditions)

    for ref_id in sorted(referenced_ids - declared_ids):
        errors.append(f"condition references indicator_id {ref_id!r}, which is not declared in indicators[].")

    return errors


def _referenced_indicator_ids(group: ConditionGroup) -> set[str]:
    ids: set[str] = set()
    for item in group.items:
        if isinstance(item, ConditionGroup):
            ids |= _referenced_indicator_ids(item)
        else:
            ids |= _referenced_indicator_ids_in_condition(item)
    return ids


def _referenced_indicator_ids_in_condition(condition: Condition) -> set[str]:
    ids: set[str] = set()
    for operand in (condition.left, condition.right):
        if isinstance(operand, IndicatorValueRef):
            ids.add(operand.indicator_id)
    return ids
