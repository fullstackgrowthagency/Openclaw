from datetime import datetime, timedelta

import pytest

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


def _evaluate(engine, signal=None, *, account_equity=10_000, account_buying_power=None, open_positions=None, snapshot=None, now=None):
    # account_buying_power defaults to account_equity -- most tests don't
    # care about the equity/buying-power distinction and just want a
    # consistent number for both.
    return engine.evaluate(
        signal or _signal(),
        account_equity=account_equity,
        account_buying_power=account_buying_power if account_buying_power is not None else account_equity,
        open_positions=open_positions or [],
        snapshot=snapshot or _snapshot(),
        now=now,
    )


def test_approves_valid_signal_with_correct_sizing():
    # max_position_size_pct pinned low here (vs. the new 100%-of-buying-power
    # default) specifically to exercise the notional cap binding before the
    # risk-based size does.
    engine = RiskEngine(RiskConfig(risk_per_trade_pct=1.0, max_position_size_pct=10.0))
    decision = _evaluate(engine)
    assert decision.approved
    # $-risk sizing alone would allow 100/0.3 = 333 shares, but a 10% of
    # buying-power notional cap ($10,000 * 10% = $1,000 at $10/share) caps
    # it lower first: 1,000 // 10 = 100 shares.
    assert decision.max_shares == 100


def test_sizing_is_risk_based_when_notional_cap_is_not_binding():
    # max_position_size_pct=100.0 is now the default, but kept explicit here
    # since the point of this test is exercising the un-capped risk-based path.
    engine = RiskEngine(RiskConfig(risk_per_trade_pct=1.0, max_position_size_pct=100.0))
    decision = _evaluate(engine)
    assert decision.approved
    # risk_amount = 1% of 10,000 = 100; per-share risk = 10 - 9.7 = 0.3 -> 333 shares
    assert decision.max_shares == 333


def test_position_size_cap_uses_buying_power_not_equity():
    # Buying power much lower than equity (e.g. capital already committed
    # elsewhere) should cap the position even though equity alone would allow more.
    engine = RiskEngine(RiskConfig(risk_per_trade_pct=100.0, max_position_size_pct=100.0))
    decision = _evaluate(engine, account_equity=10_000, account_buying_power=2_000)
    assert decision.approved
    # Buying power cap: $2,000 * 100% = $2,000 notional at $10/share -> 200 shares,
    # well below what a 100%-of-equity risk budget would otherwise allow.
    assert decision.max_shares == 200


def test_rejects_when_stop_missing_and_required():
    engine = RiskEngine(RiskConfig(stop_loss_required=True))
    decision = _evaluate(engine, _signal(suggested_stop=None))
    assert not decision.approved


def test_rejects_when_spread_too_wide():
    engine = RiskEngine(RiskConfig(max_spread_pct=0.5))
    wide_spread_snapshot = _snapshot(bid=9.0, ask=11.0)
    decision = _evaluate(engine, snapshot=wide_spread_snapshot)
    assert not decision.approved
    assert "Spread" in decision.reason


def test_rejects_when_kill_switch_active():
    engine = RiskEngine()
    engine.engage_kill_switch("manual test halt")
    decision = _evaluate(engine)
    assert not decision.approved


def test_rejects_when_daily_loss_limit_hit():
    engine = RiskEngine(RiskConfig(max_daily_loss_pct=1.0))
    engine.record_trade_closed("XYZ", pnl=-150.0)  # -1.5% of 10,000
    decision = _evaluate(engine)
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
    decision = _evaluate(engine, open_positions=[existing])
    assert not decision.approved


def test_enforces_max_trades_per_ticker_per_day():
    engine = RiskEngine(RiskConfig(max_trades_per_ticker_per_day=1, cooldown_minutes_after_loss=0))
    first = _evaluate(engine)
    assert first.approved
    second = _evaluate(engine)
    assert not second.approved


# -- record_entry_order_failed: rolling back an approved-but-never-filled
# entry -- see TradingLoop._submit_entry/_poll_pending_entry, and the real
# production bug this fixes: a broker-rejected order (e.g. outside trading
# hours) was silently consuming a real trade's slot despite no position ever
# opening, eventually exhausting max_trades_per_ticker_per_day with zero
# actual trades ------------------------------------------------------------

def test_record_entry_order_failed_frees_up_the_ticker_slot():
    engine = RiskEngine(RiskConfig(max_trades_per_ticker_per_day=1, cooldown_minutes_after_loss=0))
    first = _evaluate(engine)
    assert first.approved

    # The order this approval led to never actually filled (broker
    # rejected it) -- roll back the optimistic increment.
    engine.record_entry_order_failed("ABCD")

    second = _evaluate(engine)
    assert second.approved  # slot was freed, not permanently consumed


def test_record_entry_order_failed_frees_up_the_daily_trade_count():
    engine = RiskEngine(RiskConfig(max_trades_per_day=1, max_trades_per_ticker_per_day=10))
    first = _evaluate(engine, signal=_signal(symbol="AAAA"))
    assert first.approved

    engine.record_entry_order_failed("AAAA")

    second = _evaluate(engine, signal=_signal(symbol="BBBB"), snapshot=_snapshot(symbol="BBBB"))
    assert second.approved  # daily slot was freed too, not just the ticker one


def test_record_entry_order_failed_does_not_go_negative():
    engine = RiskEngine(RiskConfig())
    # No prior approval at all -- must not underflow into a negative count
    # that would then let extra approvals sneak through the daily cap.
    engine.record_entry_order_failed("ABCD")
    assert engine._daily.trade_count == 0
    assert engine._daily.trades_per_ticker.get("ABCD", 0) == 0


def test_record_entry_order_failed_only_frees_the_affected_ticker():
    engine = RiskEngine(RiskConfig(max_trades_per_ticker_per_day=1, cooldown_minutes_after_loss=0))
    _evaluate(engine, signal=_signal(symbol="AAAA"))
    _evaluate(engine, signal=_signal(symbol="BBBB"), snapshot=_snapshot(symbol="BBBB"))

    engine.record_entry_order_failed("AAAA")

    # AAAA's slot was freed...
    assert _evaluate(engine, signal=_signal(symbol="AAAA")).approved
    # ...but BBBB's real approval is untouched and still counts against it.
    assert not _evaluate(engine, signal=_signal(symbol="BBBB"), snapshot=_snapshot(symbol="BBBB")).approved


def test_cooldown_blocks_reentry_after_loss():
    engine = RiskEngine(RiskConfig(cooldown_minutes_after_loss=15, max_trades_per_ticker_per_day=10))
    now = datetime.utcnow()
    engine.record_trade_closed("ABCD", pnl=-10.0, now=now)
    decision = _evaluate(engine, now=now + timedelta(minutes=5))
    assert not decision.approved

    later_decision = _evaluate(engine, now=now + timedelta(minutes=16))
    assert later_decision.approved


def test_rejects_when_reward_risk_below_minimum():
    # Entry 10.0, stop 9.7 (risk 0.3/share), target 10.5 (reward 0.5/share)
    # -> ratio 1.67, below the default 2.0 minimum.
    engine = RiskEngine(RiskConfig(min_risk_reward_ratio=2.0))
    decision = _evaluate(engine, _signal(suggested_target=10.5))
    assert not decision.approved
    assert "Reward:risk" in decision.reason


def test_approves_when_reward_risk_meets_minimum_exactly():
    # risk 0.3/share, target 10.6 -> reward 0.6/share -> ratio exactly 2.0.
    engine = RiskEngine(RiskConfig(min_risk_reward_ratio=2.0, max_position_size_pct=100.0))
    decision = _evaluate(engine, _signal(suggested_target=10.6))
    assert decision.approved


def test_reward_risk_check_skipped_when_no_target():
    # VolumeIgnitionStrategy-style signal: no suggested_target at all --
    # the min R:R gate has nothing to compare against and must not reject it.
    engine = RiskEngine(RiskConfig(min_risk_reward_ratio=2.0, max_position_size_pct=100.0))
    decision = _evaluate(engine, _signal(suggested_target=None))
    assert decision.approved


def test_rejects_when_total_assumed_risk_exhausted():
    # Existing position's own stop-loss risk already consumes the entire
    # total-risk ceiling: (5.0 - 4.9) * 10,000 shares = $1,000 = 50% of a
    # $2,000 equity account (max_total_risk_pct default 50%).
    engine = RiskEngine(RiskConfig(max_total_risk_pct=50.0))
    existing = Position(
        symbol="EFGH",
        side=OrderSide.BUY,
        quantity=10_000,
        avg_entry_price=5.0,
        stop_price=4.9,
        target_price=None,
        trailing_stop_pct=None,
        opened_at=datetime.utcnow(),
        strategy_name="test",
    )
    decision = _evaluate(engine, account_equity=2_000, open_positions=[existing])
    assert not decision.approved
    assert "risk" in decision.reason.lower()


def test_total_assumed_risk_shrinks_new_trades_budget():
    # Existing position already assumes $400 of risk. Total ceiling is 50%
    # of $2,000 equity = $1,000, leaving only $600 of room -- even though
    # risk_per_trade_pct alone would budget $1,000 (50% of equity) for this
    # new trade, it gets shrunk to the $600 remaining.
    engine = RiskEngine(RiskConfig(max_total_risk_pct=50.0, risk_per_trade_pct=50.0, max_position_size_pct=100.0))
    existing = Position(
        symbol="EFGH",
        side=OrderSide.BUY,
        quantity=1_000,
        avg_entry_price=5.0,
        stop_price=4.6,  # $0.40/share * 1,000 = $400 assumed risk
        target_price=None,
        trailing_stop_pct=None,
        opened_at=datetime.utcnow(),
        strategy_name="test",
    )
    decision = _evaluate(engine, account_equity=2_000, open_positions=[existing])
    assert decision.approved
    # Remaining risk room = 1,000 - 400 = 600; per-share risk = 10 - 9.7 = 0.3
    # -> 2,000 shares by risk, well above the notional cap, so risk_amount
    # itself should reflect the shrunk $600, not the full $1,000 budget.
    assert decision.risk_amount == pytest.approx(600.0)
