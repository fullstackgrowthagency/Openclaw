import pytest

from fx_bot.strategy_builder.validator import StrategyConfigError, validate_strategy_config


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


def test_a_well_formed_config_validates_cleanly():
    config = validate_strategy_config(_ema_cross_config())
    assert config.pair == "EUR/USD"


def test_structural_errors_are_wrapped_in_strategy_config_error():
    with pytest.raises(StrategyConfigError):
        validate_strategy_config(_ema_cross_config(pair="not-a-pair"))


def test_unknown_indicator_type_is_rejected():
    config = _ema_cross_config(indicators=[{"id": "fast", "type": "not_a_real_indicator", "params": {}}])
    with pytest.raises(StrategyConfigError) as exc_info:
        validate_strategy_config(config)
    assert any("unknown indicator type" in m for m in exc_info.value.messages)


def test_missing_required_indicator_param_is_rejected():
    config = _ema_cross_config(indicators=[
        {"id": "fast", "type": "ema", "params": {}},  # missing 'period'
        {"id": "slow", "type": "ema", "params": {"period": 20}},
    ])
    with pytest.raises(StrategyConfigError) as exc_info:
        validate_strategy_config(config)
    assert any("missing required param" in m for m in exc_info.value.messages)


def test_condition_referencing_an_undeclared_indicator_is_rejected():
    config = _ema_cross_config(entry_conditions={
        "op": "and",
        "items": [{
            "left": {"kind": "indicator", "indicator_id": "not_declared"},
            "operator": "gt",
            "right": {"kind": "constant", "value": 1},
        }],
    })
    with pytest.raises(StrategyConfigError) as exc_info:
        validate_strategy_config(config)
    assert any("not_declared" in m for m in exc_info.value.messages)


def test_exit_conditions_indicator_references_are_also_checked():
    config = _ema_cross_config(exit_conditions={
        "op": "and",
        "items": [{
            "left": {"kind": "indicator", "indicator_id": "ghost"},
            "operator": "lt",
            "right": {"kind": "constant", "value": 0},
        }],
    })
    with pytest.raises(StrategyConfigError) as exc_info:
        validate_strategy_config(config)
    assert any("ghost" in m for m in exc_info.value.messages)
