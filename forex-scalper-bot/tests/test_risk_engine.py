from datetime import datetime

from fx_bot.enums import SignalAction
from fx_bot.models import MarketSnapshot, Position, Signal
from fx_bot.risk.risk_engine import RiskConfig, RiskEngine
from fx_bot.enums import OrderSide


def _signal(**overrides) -> Signal:
    base = dict(
        symbol="EUR/USD", action=SignalAction.ENTER_LONG, generated_at=datetime.utcnow(),
        strategy_name="test", strategy_version="v1", reference_price=1.1000, suggested_stop=1.0980,
    )
    base.update(overrides)
    return Signal(**base)


def _snapshot() -> MarketSnapshot:
    return MarketSnapshot(symbol="EUR/USD", timestamp=datetime.utcnow(), bid=1.0999, ask=1.1001)


def _position(symbol="EUR/USD") -> Position:
    return Position(
        symbol=symbol, side=OrderSide.BUY, quantity=10_000, avg_entry_price=1.1000,
        stop_price=1.0980, target_price=1.1040, trailing_stop_pips=None,
        opened_at=datetime.utcnow(), strategy_name="test",
    )


def test_approves_a_valid_entry_with_a_stop():
    engine = RiskEngine()
    decision = engine.evaluate(_signal(), open_positions=[], snapshot=_snapshot())
    assert decision.approved
    assert decision.max_units == RiskConfig().default_quantity


def test_rejects_entry_missing_a_stop_when_required():
    engine = RiskEngine(RiskConfig(stop_loss_required=True))
    decision = engine.evaluate(_signal(suggested_stop=None), open_positions=[], snapshot=_snapshot())
    assert not decision.approved
    assert "stop" in decision.reason.lower()


def test_allows_missing_stop_when_not_required():
    engine = RiskEngine(RiskConfig(stop_loss_required=False))
    decision = engine.evaluate(_signal(suggested_stop=None), open_positions=[], snapshot=_snapshot())
    assert decision.approved


def test_rejects_when_max_simultaneous_positions_reached():
    engine = RiskEngine(RiskConfig(max_simultaneous_positions=1))
    decision = engine.evaluate(_signal(), open_positions=[_position()], snapshot=_snapshot())
    assert not decision.approved
    assert "max_simultaneous_positions" in decision.reason


def test_evaluate_rejects_non_entry_actions_since_it_only_gates_entries():
    # evaluate() itself has no meaningful answer for EXIT/SCALE_IN/SCALE_OUT
    # -- OrderManager.submit_signal never calls it for those (see
    # test_order_manager.py's test_exit_is_never_gated_by_the_risk_engine),
    # this just documents evaluate()'s own contract if called directly.
    engine = RiskEngine()
    decision = engine.evaluate(_signal(action=SignalAction.EXIT), open_positions=[], snapshot=_snapshot())
    assert not decision.approved
    assert "only gates entries" in decision.reason
