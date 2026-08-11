"""
End-to-end wiring test: broad-scanner-style seeding -> CandidateWatcher ->
TriggerEngine -> MomentumBreakoutStrategy -> RiskEngine -> OrderManager ->
PaperBrokerClient -> PositionManager exit, all through BacktestEngine.

State-transition thresholds are deliberately loosened (vs. the production
defaults in weights.yaml) so this test exercises the full pipeline
deterministically without depending on the exact MIS formula, which is
covered separately in test_momentum_score.py.
"""
from datetime import datetime, timedelta

from webull_bot.backtest.engine import BacktestEngine
from webull_bot.enums import ExitReason
from webull_bot.models import FloatData, MarketSnapshot
from webull_bot.risk.risk_engine import RiskConfig
from webull_bot.scanner.candidate_watcher import WatcherConfig
from webull_bot.strategy.momentum_breakout import MomentumBreakoutStrategy


def _snapshot(t, last_price, high_of_day, low_of_day, cumulative_volume, bid, ask, vwap) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="TEST",
        timestamp=t,
        last_price=last_price,
        bid=bid,
        ask=ask,
        bid_size=500,
        ask_size=500,
        cumulative_volume=cumulative_volume,
        vwap=vwap,
        high_of_day=high_of_day,
        low_of_day=low_of_day,
        open_price=5.00,
    )


def _build_bars() -> list[MarketSnapshot]:
    # 14:31 UTC = 9:31am US/Eastern in January (EST, UTC-5) -- a real,
    # deterministic weekday (Monday) time within core trading hours, needed
    # now that a backtest's simulated bar timestamp (snapshot.timestamp) is
    # what RiskEngine.evaluate's core-hours gate checks against (see
    # backtest/engine.py's submit_signal(..., now=snapshot.timestamp)),
    # rather than the real wall clock the backtest happens to run at.
    t0 = datetime(2026, 1, 5, 14, 31, 0)
    bars = [
        _snapshot(t0, 5.00, 5.05, 4.95, 150_000, 4.99, 5.01, 5.00),
        _snapshot(t0 + timedelta(minutes=1), 5.10, 5.10, 4.95, 250_000, 5.09, 5.11, 5.05),
        _snapshot(t0 + timedelta(minutes=2), 5.20, 5.20, 4.95, 600_000, 5.19, 5.21, 5.15),
        _snapshot(t0 + timedelta(minutes=3), 5.60, 5.60, 4.95, 650_000, 5.59, 5.61, 5.50),
    ]
    return bars


def _float_data() -> FloatData:
    return FloatData(
        symbol="TEST",
        free_float_shares=3_000_000,
        shares_outstanding=4_000_000,
        market_cap=15_000_000,
        float_percent=0.75,
        effective_date=None,
        fetched_at=datetime.utcnow(),
    )


def test_full_pipeline_enters_on_breakout_and_partially_exits_on_target():
    # Hitting target no longer fully closes the position -- it sells half
    # (SCALE_OUT) and lets the remainder ride on the trailing
    # stop/breakeven rules instead (see docs/ARCHITECTURE.md's "Position
    # management" section). The candidate stays MANAGING with a reduced
    # open position rather than moving to COOLDOWN.
    engine = BacktestEngine(
        # max_position_size_pct pinned below the 100%-of-buying-power default
        # so the sized order's notional (computed off the signal's reference
        # price) still clears PaperBrokerClient's cash check once the actual
        # fill price is nudged up by simulated slippage -- sizing at exactly
        # 100% of buying power leaves zero room for that and the order gets
        # rejected for insufficient funds before ever filling.
        strategies=[MomentumBreakoutStrategy()],
        risk_config=RiskConfig(stop_loss_required=True, max_position_size_pct=90.0),
        watcher_config=WatcherConfig(heating_up_score_threshold=15.0, armed_score_threshold=35.0),
    )

    result = engine.run({"TEST": _build_bars()}, {"TEST": _float_data()})

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.symbol == "TEST"
    assert trade.exit_reason == ExitReason.PARTIAL_PROFIT_TARGET
    assert trade.pnl > 0
    assert result.ending_equity > engine.config.starting_equity

    candidate = engine.candidates["TEST"]
    assert candidate.state.value == "managing"

    remaining_position = next(p for p in engine.broker.get_positions() if p.symbol == "TEST")
    assert remaining_position.partial_exit_taken is True
    assert remaining_position.quantity > 0


def test_no_trade_when_thresholds_never_reached():
    engine = BacktestEngine(
        strategies=[MomentumBreakoutStrategy()],
        watcher_config=WatcherConfig(heating_up_score_threshold=99.0, armed_score_threshold=99.9),
    )
    result = engine.run({"TEST": _build_bars()}, {"TEST": _float_data()})
    assert len(result.trades) == 0
