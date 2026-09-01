import pytest
from pydantic import ValidationError

from fx_bot.strategy_builder.schema import StrategyConfig


def _ema_cross_config(**overrides) -> dict:
    base = dict(
        id="t1", name="test", pair="EUR/USD", entry_side="long",
        indicators=[
            {"id": "fast", "type": "ema", "params": {"period": 5}},
            {"id": "slow", "type": "ema", "params": {"period": 20}},
        ],
        entry_conditions={
            "op": "and",
            "items": [{
                "left": {"kind": "indicator", "indicator_id": "fast"},
                "operator": "crosses_above",
                "right": {"kind": "indicator", "indicator_id": "slow"},
            }],
        },
        stop_loss={"pips": 10},
        take_profit={"type": "fixed_pips", "pips": 20},
    )
    base.update(overrides)
    return base


def test_a_well_formed_config_parses():
    config = StrategyConfig(**_ema_cross_config())
    assert config.pair == "EUR/USD"
    assert config.version == 1


def test_malformed_pair_is_rejected():
    with pytest.raises(ValidationError):
        StrategyConfig(**_ema_cross_config(pair="EURUSD"))


def test_not_group_requires_exactly_one_item():
    with pytest.raises(ValidationError):
        StrategyConfig(**_ema_cross_config(entry_conditions={
            "op": "not",
            "items": [
                {"left": {"kind": "constant", "value": 1}, "operator": "eq", "right": {"kind": "constant", "value": 1}},
                {"left": {"kind": "constant", "value": 2}, "operator": "eq", "right": {"kind": "constant", "value": 2}},
            ],
        }))


def test_empty_condition_group_is_rejected():
    with pytest.raises(ValidationError):
        StrategyConfig(**_ema_cross_config(entry_conditions={"op": "and", "items": []}))


def test_fixed_pips_take_profit_requires_pips():
    with pytest.raises(ValidationError):
        StrategyConfig(**_ema_cross_config(take_profit={"type": "fixed_pips"}))


def test_risk_reward_ratio_take_profit_requires_ratio():
    with pytest.raises(ValidationError):
        StrategyConfig(**_ema_cross_config(take_profit={"type": "risk_reward_ratio"}))


def test_risk_reward_ratio_take_profit_accepts_a_ratio():
    config = StrategyConfig(**_ema_cross_config(take_profit={"type": "risk_reward_ratio", "ratio": 2.0}))
    assert config.take_profit.ratio == 2.0


def test_nested_condition_groups_parse():
    nested = _ema_cross_config(entry_conditions={
        "op": "or",
        "items": [
            {"left": {"kind": "constant", "value": 1}, "operator": "eq", "right": {"kind": "constant", "value": 1}},
            {"op": "and", "items": [
                {"left": {"kind": "constant", "value": 2}, "operator": "gt", "right": {"kind": "constant", "value": 1}},
            ]},
        ],
    })
    config = StrategyConfig(**nested)
    assert config.entry_conditions.op == "or"
    assert len(config.entry_conditions.items) == 2
