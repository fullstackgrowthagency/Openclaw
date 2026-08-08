from datetime import datetime, timedelta

from webull_bot.enums import OrderSide, SignalAction
from webull_bot.models import MarketSnapshot, Position, Signal
from webull_bot.risk.risk_engine import RiskConfig, RiskEngine


def _snapshot(**overrides) -> MarketSnapshot:
    base = dict(
        symbol="ABCD",
        timestamp=datetime.utcnow(),
        last_price=10.0,
        bid=9.99,
        ask=10.01,
        bid_size=100,
        ask_size=100,
        cumulative_volume=200_000,
        vwap=9.8,
        high_of_day=10.1,
        low_of_day=9.5,
        open_price=9.6,
    )
    base.update(overrides)
    return MarketSnapshot(**base)


def _signal(**overrides) -> Signal:
    base = dict(
        symbol="ABCD",
        action=SignalAction.ENTER_LONG,
        generated_at=datetime.utcnow(),
        strategy_name="test",
        strategy_version="v1",
        reference_price=10.0,
        suggested_stop=9.7,
    )
    base.update(overrides)
    return Signal(**base)


def test_approves_valid_signal_with_correct_sizing():
    engine = RiskEngine(RiskConfig(risk_per_trade_pct=1.0))
    decision = engine.evaluate(_signal(), account_equity=10_000, open_positions=[], snapshot=_snapshot())
    assert decision.approved
    # $-risk sizing alone would allow 100/0.3 = 333 shares, but the default
    # max_position_size_pct (10% of equity = $1,000 notional at $10/share)
    # caps it lower first: 1,000 // 10 = 100 shares.
    assert decision.max_shares == 100


def test_sizing_is_risk_based_when_notional_cap_is_not_binding():
    engine = RiskEngine(RiskConfig(risk_per_trade_pct=1.0, max_position_size_pct=100.0))
    decision = engine.evaluate(_signal(), account_equity=10_000, open_positions=[], snapshot=_snapshot())
    assert decision.approved
    # risk_amount = 1% of 10,000 = 100; per-share risk = 10 - 9.7 = 0.3 -> 333 shares
    assert decision.max_shares == 333


def test_rejects_when_stop_missing_and_required():
    engine = RiskEngine(RiskConfig(stop_loss_required=True))
    decision = engine.evaluate(_signal(suggested_stop=None), account_equity=10_000, open_positions=[], snapshot=_snapshot())
    assert not decision.approved


def test_rejects_when_spread_too_wide():
    engine = RiskEngine(RiskConfig(max_spread_pct=0.5))
    wide_spread_snapshot = _snapshot(bid=9.0, ask=11.0)
    decision = engine.evaluate(_signal(), account_equity=10_000, open_positions=[], snapshot=wide_spread_snapshot)
    assert not decision.approved
    assert "Spread" in decision.reason


def test_rejects_when_kill_switch_active():
    engine = RiskEngine()
    engine.engage_kill_switch("manual test halt")
    decision = engine.evaluate(_signal(), account_equity=10_000, open_positions=[], snapshot=_snapshot())
    assert not decision.approved


def test_rejects_when_daily_loss_limit_hit():
    engine = RiskEngine(RiskConfig(max_daily_loss_pct=1.0))
    engine.record_trade_closed("XYZ", pnl=-150.0)  # -1.5% of 10,000
    decision = engine.evaluate(_signal(), account_equity=10_000, open_positions=[], snapshot=_snapshot())
    assert not decision.approved


def test_rejects_when_max_positions_hit():
    engine = RiskEngine(RiskConfig(max_simultaneous_positions=1))
    existing = Position(
        symbol="EFGH",
        side=OrderSide.BUY,
        quantity=100,
        avg_entry_price=5.0,
        stop_price=4.5,
        target_price=None,
        trailing_stop_pct=None,
        opened_at=datetime.utcnow(),
        strategy_name="test",
    )
    decision = engine.evaluate(_signal(), account_equity=10_000, open_positions=[existing], snapshot=_snapshot())
    assert not decision.approved


def test_enforces_max_trades_per_ticker_per_day():
    engine = RiskEngine(RiskConfig(max_trades_per_ticker_per_day=1, cooldown_minutes_after_loss=0))
    first = engine.evaluate(_signal(), account_equity=10_000, open_positions=[], snapshot=_snapshot())
    assert first.approved
    second = engine.evaluate(_signal(), account_equity=10_000, open_positions=[], snapshot=_snapshot())
    assert not second.approved


def test_cooldown_blocks_reentry_after_loss():
    engine = RiskEngine(RiskConfig(cooldown_minutes_after_loss=15, max_trades_per_ticker_per_day=10))
    now = datetime.utcnow()
    engine.record_trade_closed("ABCD", pnl=-10.0, now=now)
    decision = engine.evaluate(_signal(), account_equity=10_000, open_positions=[], snapshot=_snapshot(), now=now + timedelta(minutes=5))
    assert not decision.approved

    later_decision = engine.evaluate(
        _signal(), account_equity=10_000, open_positions=[], snapshot=_snapshot(), now=now + timedelta(minutes=16)
    )
    assert later_decision.approved
