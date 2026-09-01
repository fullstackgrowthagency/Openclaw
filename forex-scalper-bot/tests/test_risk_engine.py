from datetime import datetime, timedelta

import pytest

from fx_bot.enums import OrderSide, SignalAction
from fx_bot.models import MarketSnapshot, Position, Signal
from fx_bot.risk.risk_engine import RiskConfig, RiskEngine

# Wednesday 2026-09-02 15:00 UTC -- inside the London/New York overlap and
# a real weekday, so tests don't depend on when the suite happens to run.
_NOW = datetime(2026, 9, 2, 15, 0, 0)


def _signal(**overrides) -> Signal:
    base = dict(
        symbol="EUR/USD", action=SignalAction.ENTER_LONG, generated_at=_NOW,
        strategy_name="test", strategy_version="v1", reference_price=1.1000, suggested_stop=1.0980,
    )
    base.update(overrides)
    return Signal(**base)


def _snapshot(symbol="EUR/USD", bid=1.0999, ask=1.1001) -> MarketSnapshot:
    return MarketSnapshot(symbol=symbol, timestamp=_NOW, bid=bid, ask=ask)


def _position(symbol="EUR/USD", side=OrderSide.BUY, stop_price=1.0980, avg_entry_price=1.1000) -> Position:
    return Position(
        symbol=symbol, side=side, quantity=10_000, avg_entry_price=avg_entry_price,
        stop_price=stop_price, target_price=1.1040, trailing_stop_pips=None,
        opened_at=_NOW, strategy_name="test",
    )


def _evaluate(engine, signal=None, *, account_equity=10_000.0, open_positions=None, snapshot=None, now=_NOW):
    return engine.evaluate(
        signal or _signal(), account_equity=account_equity, open_positions=open_positions or [],
        snapshot=snapshot or _snapshot(), now=now,
    )


# -- basic contract ----------------------------------------------------------

def test_evaluate_rejects_non_entry_actions_since_it_only_gates_entries():
    engine = RiskEngine()
    decision = _evaluate(engine, _signal(action=SignalAction.EXIT))
    assert not decision.approved
    assert "only gates entries" in decision.reason


def test_approves_a_well_formed_entry_by_default():
    engine = RiskEngine()
    decision = _evaluate(engine)
    assert decision.approved
    assert decision.max_units > 0


# -- session windows -----------------------------------------------------

def test_rejects_when_market_is_closed_and_no_session_restriction_configured():
    engine = RiskEngine()
    saturday = datetime(2026, 9, 5, 12, 0, 0)
    decision = _evaluate(engine, now=saturday)
    assert not decision.approved


def test_rejects_outside_a_configured_session_window():
    engine = RiskEngine(RiskConfig(session_windows=("tokyo",)))
    decision = _evaluate(engine, now=_NOW)  # _NOW is london/new_york, not tokyo
    assert not decision.approved


def test_approves_inside_a_configured_session_window():
    engine = RiskEngine(RiskConfig(session_windows=("london",)))
    decision = _evaluate(engine, now=_NOW)
    assert decision.approved


def test_london_new_york_overlap_is_a_distinct_tighter_window():
    engine = RiskEngine(RiskConfig(session_windows=("london_new_york_overlap",)))
    inside_overlap = _NOW  # 15:00 UTC
    london_only = datetime(2026, 9, 2, 9, 0, 0)  # london open, before the overlap starts
    assert _evaluate(engine, now=inside_overlap).approved
    assert not _evaluate(engine, now=london_only).approved


# -- daily limits --------------------------------------------------------

def test_rejects_when_daily_loss_limit_reached():
    engine = RiskEngine(RiskConfig(max_daily_loss_pct=1.0))
    engine.record_trade_closed("EUR/USD", pnl=-150.0, now=_NOW)  # 1.5% of 10,000 equity
    decision = _evaluate(engine, account_equity=10_000.0)
    assert not decision.approved


def test_rejects_when_max_trades_per_day_reached():
    engine = RiskEngine(RiskConfig(max_trades_per_day=1))
    assert _evaluate(engine).approved
    assert not _evaluate(engine).approved


def test_rejects_when_max_trades_per_pair_per_day_reached():
    engine = RiskEngine(RiskConfig(max_trades_per_day=100, max_trades_per_pair_per_day=1))
    assert _evaluate(engine, _signal(symbol="EUR/USD")).approved
    assert not _evaluate(engine, _signal(symbol="EUR/USD")).approved
    assert _evaluate(engine, _signal(symbol="GBP/USD"), snapshot=_snapshot("GBP/USD")).approved


def test_daily_state_resets_on_a_new_day():
    engine = RiskEngine(RiskConfig(max_trades_per_day=1))
    assert _evaluate(engine, now=_NOW).approved
    next_day = _NOW + timedelta(days=1)
    assert _evaluate(engine, now=next_day).approved  # would be rejected without the day roll


def test_cooldown_blocks_the_same_pair_after_a_loss():
    engine = RiskEngine(RiskConfig(cooldown_minutes_after_loss=30))
    engine.record_trade_closed("EUR/USD", pnl=-50.0, now=_NOW)
    still_cooling = _evaluate(engine, now=_NOW + timedelta(minutes=10))
    cooled_off = _evaluate(engine, now=_NOW + timedelta(minutes=31))
    assert not still_cooling.approved
    assert cooled_off.approved


def test_cooldown_does_not_affect_a_different_pair():
    engine = RiskEngine(RiskConfig(cooldown_minutes_after_loss=30))
    engine.record_trade_closed("EUR/USD", pnl=-50.0, now=_NOW)
    decision = _evaluate(engine, _signal(symbol="GBP/USD"), snapshot=_snapshot("GBP/USD"), now=_NOW + timedelta(minutes=1))
    assert decision.approved


def test_a_winning_trade_does_not_trigger_cooldown():
    engine = RiskEngine(RiskConfig(cooldown_minutes_after_loss=30))
    engine.record_trade_closed("EUR/USD", pnl=+50.0, now=_NOW)
    decision = _evaluate(engine, now=_NOW + timedelta(minutes=1))
    assert decision.approved


# -- exposure caps ---------------------------------------------------------

def test_rejects_when_max_simultaneous_positions_reached():
    engine = RiskEngine(RiskConfig(max_simultaneous_positions=1))
    decision = _evaluate(engine, open_positions=[_position(symbol="GBP/USD")])
    assert not decision.approved


def test_rejects_when_max_positions_per_pair_reached_even_under_the_overall_cap():
    engine = RiskEngine(RiskConfig(max_simultaneous_positions=5, max_positions_per_pair=1))
    decision = _evaluate(engine, _signal(symbol="EUR/USD"), open_positions=[_position(symbol="EUR/USD")])
    assert not decision.approved


def test_a_second_pair_is_unaffected_by_the_first_pairs_per_pair_cap():
    engine = RiskEngine(RiskConfig(max_simultaneous_positions=5, max_positions_per_pair=1))
    decision = _evaluate(
        engine, _signal(symbol="GBP/USD"), snapshot=_snapshot("GBP/USD"),
        open_positions=[_position(symbol="EUR/USD")],
    )
    assert decision.approved


def test_rejects_correlated_currency_exposure():
    # EUR/USD and GBP/USD share USD exposure -- opening EUR/USD while a
    # GBP/USD position is already open should count as correlated.
    engine = RiskEngine(RiskConfig(max_simultaneous_positions=5, max_correlated_pair_exposure=1))
    decision = _evaluate(engine, _signal(symbol="EUR/USD"), open_positions=[_position(symbol="GBP/USD")])
    assert not decision.approved


def test_uncorrelated_pairs_do_not_count_against_each_other():
    engine = RiskEngine(RiskConfig(max_simultaneous_positions=5, max_correlated_pair_exposure=1))
    decision = _evaluate(engine, _signal(symbol="EUR/USD"), open_positions=[_position(symbol="USD/JPY")])
    # USD/JPY shares USD with EUR/USD too (quote vs base) -- pick a pair
    # with genuinely no shared currency to prove the negative case.
    decision_unrelated = _evaluate(engine, _signal(symbol="EUR/GBP"), open_positions=[_position(symbol="USD/JPY")])
    assert not decision.approved  # EUR/USD vs USD/JPY DO share USD
    assert decision_unrelated.approved  # EUR/GBP vs USD/JPY share nothing


def test_rejects_when_max_total_risk_pct_reached():
    engine = RiskEngine(RiskConfig(max_simultaneous_positions=5, max_total_risk_pct=1.0))
    # An existing position risking 200 * 10,000... scaled down: risk = |entry-stop|*qty
    big_risk_position = _position(symbol="GBP/USD", avg_entry_price=1.3000, stop_price=1.2990)  # 0.0010 * 10_000 = 10.0
    decision = _evaluate(engine, account_equity=100.0, open_positions=[big_risk_position])  # 1% of 100 = 1.0 < 10.0 already open
    assert not decision.approved


# -- entry-quality filters ---------------------------------------------------

def test_rejects_entry_missing_a_stop_when_required():
    engine = RiskEngine(RiskConfig(stop_loss_required=True))
    decision = _evaluate(engine, _signal(suggested_stop=None))
    assert not decision.approved


def test_allows_missing_stop_when_not_required_and_sizes_with_fixed_units():
    engine = RiskEngine(RiskConfig(stop_loss_required=False, sizing_method="fixed_units", fixed_units=5_000.0))
    decision = _evaluate(engine, _signal(suggested_stop=None))
    assert decision.approved
    assert decision.max_units == 5_000.0


def test_rejects_when_spread_exceeds_max_spread_pips():
    engine = RiskEngine(RiskConfig(max_spread_pips=1.0))
    wide_spread = _snapshot(bid=1.0995, ask=1.1005)  # 10 pips
    decision = _evaluate(engine, snapshot=wide_spread)
    assert not decision.approved


def test_rejects_when_reward_risk_ratio_is_below_the_minimum():
    engine = RiskEngine(RiskConfig(min_risk_reward_ratio=3.0))
    decision = _evaluate(engine, _signal(suggested_stop=1.0980, suggested_target=1.1010))  # only ~1.5:1
    assert not decision.approved


def test_approves_when_reward_risk_ratio_meets_the_minimum():
    engine = RiskEngine(RiskConfig(min_risk_reward_ratio=2.0))
    decision = _evaluate(engine, _signal(suggested_stop=1.0980, suggested_target=1.1040))  # 2:1 exactly
    assert decision.approved


# -- position sizing ----------------------------------------------------

def test_risk_percent_sizing_scales_with_equity_and_stop_distance():
    engine = RiskEngine(RiskConfig(sizing_method="risk_percent", risk_percent_of_equity=1.0))
    # stop distance = 0.0020 (1.1000 -> 1.0980), risk_amount = 1% of 20,000 = 200
    decision = _evaluate(engine, _signal(suggested_stop=1.0980), account_equity=20_000.0)
    assert decision.approved
    assert decision.max_units == pytest.approx(200.0 / 0.0020)


def test_risk_percent_sizing_rejects_when_no_stop_is_present():
    engine = RiskEngine(RiskConfig(stop_loss_required=False, sizing_method="risk_percent"))
    decision = _evaluate(engine, _signal(suggested_stop=None))
    assert not decision.approved  # can't size risk-% of nothing


def test_fixed_units_sizing_ignores_stop_distance_entirely():
    engine = RiskEngine(RiskConfig(sizing_method="fixed_units", fixed_units=25_000.0))
    decision = _evaluate(engine, _signal(suggested_stop=1.0999))  # a tiny, almost-irrelevant stop distance
    assert decision.approved
    assert decision.max_units == 25_000.0


def test_approved_decision_reports_risk_amount():
    engine = RiskEngine(RiskConfig(sizing_method="fixed_units", fixed_units=10_000.0))
    decision = _evaluate(engine, _signal(reference_price=1.1000, suggested_stop=1.0980))
    assert decision.risk_amount == pytest.approx(0.0020 * 10_000.0)
