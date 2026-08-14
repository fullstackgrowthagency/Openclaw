"""
Tests for CandidateWatcher.update_resistance's merge of the running
intraday high with static (volume-profile-derived) resistance levels --
see scanner/candidate_watcher.py's docstring for the "nearest untested
ceiling" rationale. update()'s score/state-transition behavior has
integration coverage via test_trading_loop.py/test_backtest_engine.py;
this file is specifically about the resistance-merge logic that changed,
candidate.last_price bookkeeping (dashboard/app.py's Price column reads
that field), and the temporary trade_eligible/block_reasons behavior that
replaced permanent REJECTED-on-spread/liquidity (see module docstring).
"""
from datetime import datetime, timedelta

from webull_bot.enums import CandidateState, TradeBlockReason
from webull_bot.models import Candidate
from webull_bot.scanner.candidate_watcher import CandidateWatcher
from webull_bot.state_machine import new_candidate, transition


def _snapshot(high_of_day: float):
    from webull_bot.models import MarketSnapshot
    return MarketSnapshot(
        symbol="TEST", timestamp=datetime.utcnow(), last_price=high_of_day, bid=high_of_day - 0.01,
        ask=high_of_day + 0.01, bid_size=100, ask_size=100, cumulative_volume=100_000, vwap=high_of_day,
        high_of_day=high_of_day, low_of_day=high_of_day, open_price=high_of_day,
    )


def _snapshot_with_conditions(price: float, spread_pct: float, cumulative_volume: float):
    """Builds a snapshot with a precisely-controlled spread_pct and dollar
    volume (price * cumulative_volume), for exercising
    CandidateWatcher.update()'s trade_eligible/block_reasons logic."""
    from webull_bot.models import MarketSnapshot
    half_spread = price * (spread_pct / 100.0) / 2.0
    return MarketSnapshot(
        symbol="TEST", timestamp=datetime.utcnow(), last_price=price, bid=price - half_spread,
        ask=price + half_spread, bid_size=100, ask_size=100, cumulative_volume=cumulative_volume,
        vwap=price, high_of_day=price, low_of_day=price, open_price=price,
    )


def _candidate(static_resistance_levels=None, resistance_level=None) -> Candidate:
    candidate = new_candidate("TEST")
    candidate.static_resistance_levels = static_resistance_levels or []
    candidate.resistance_level = resistance_level
    return candidate


def test_update_resistance_with_no_static_levels_uses_plain_running_high():
    # Regression: this is update_resistance's entire behavior before static
    # levels existed, and must still hold for paper/backtest candidates
    # that never get any (see BroadScanner._compute_static_resistance_levels).
    watcher = CandidateWatcher()
    candidate = _candidate(static_resistance_levels=[])
    watcher.update_resistance(candidate, _snapshot(10.0))
    assert candidate.resistance_level == 10.0


def test_update_resistance_running_high_never_decreases():
    watcher = CandidateWatcher()
    candidate = _candidate(resistance_level=10.0)
    watcher.update_resistance(candidate, _snapshot(8.0))  # a lower high than before
    assert candidate.resistance_level == 10.0


def test_update_resistance_picks_nearest_static_level_above_running_high():
    watcher = CandidateWatcher()
    candidate = _candidate(static_resistance_levels=[9.0, 12.0, 20.0])
    watcher.update_resistance(candidate, _snapshot(10.0))
    # 9.0 is already behind the running high; 12.0 is the nearest one ahead.
    assert candidate.resistance_level == 12.0


def test_update_resistance_falls_back_to_running_high_once_all_static_levels_are_cleared():
    watcher = CandidateWatcher()
    candidate = _candidate(static_resistance_levels=[9.0, 9.5])
    watcher.update_resistance(candidate, _snapshot(10.0))
    assert candidate.resistance_level == 10.0


def test_update_resistance_static_level_wins_over_stale_running_high():
    # resistance_level was already 12.0 from a prior tick (higher than this
    # tick's snapshot high), but a static level of 15.0 should still take
    # over as the nearest real ceiling once it's the closest one ahead.
    watcher = CandidateWatcher()
    candidate = _candidate(static_resistance_levels=[15.0], resistance_level=12.0)
    watcher.update_resistance(candidate, _snapshot(11.0))
    assert candidate.resistance_level == 15.0


def test_update_records_the_latest_price():
    watcher = CandidateWatcher()
    candidate = _candidate()
    transition(candidate, CandidateState.WATCHING)
    watcher.update(candidate, _snapshot(6.5))
    assert candidate.last_price == 6.5


def test_update_does_not_touch_last_price_once_rejected():
    watcher = CandidateWatcher()
    candidate = _candidate()
    transition(candidate, CandidateState.WATCHING)
    candidate.last_price = 4.0
    transition(candidate, CandidateState.REJECTED, reason="test")
    watcher.update(candidate, _snapshot(9.0))
    assert candidate.last_price == 4.0  # update() returns early for REJECTED, nothing should change


# -- trade_eligible / block_reasons (temporary, not permanent, blocking) -----

def test_update_blocks_trade_eligibility_on_wide_spread_without_rejecting():
    watcher = CandidateWatcher()
    candidate = _candidate()
    transition(candidate, CandidateState.WATCHING)
    snapshot = _snapshot_with_conditions(price=10.0, spread_pct=5.0, cumulative_volume=1_000_000)  # > default max_spread_pct=2.0
    watcher.update(candidate, snapshot)
    assert candidate.trade_eligible is False
    assert candidate.block_reasons == [TradeBlockReason.SPREAD_TOO_WIDE]
    assert candidate.state == CandidateState.WATCHING  # NOT rejected


def test_update_blocks_trade_eligibility_on_low_liquidity_without_rejecting():
    watcher = CandidateWatcher()
    candidate = _candidate()
    transition(candidate, CandidateState.WATCHING)
    # price 10 * volume 1_000 = $10,000 dollar volume, well under default min_dollar_volume=500_000
    snapshot = _snapshot_with_conditions(price=10.0, spread_pct=0.1, cumulative_volume=1_000)
    watcher.update(candidate, snapshot)
    assert candidate.trade_eligible is False
    assert candidate.block_reasons == [TradeBlockReason.LOW_LIQUIDITY]
    assert candidate.state == CandidateState.WATCHING  # NOT rejected


def test_update_records_both_block_reasons_when_both_conditions_fail():
    watcher = CandidateWatcher()
    candidate = _candidate()
    transition(candidate, CandidateState.WATCHING)
    snapshot = _snapshot_with_conditions(price=10.0, spread_pct=5.0, cumulative_volume=1_000)
    watcher.update(candidate, snapshot)
    assert set(candidate.block_reasons) == {TradeBlockReason.SPREAD_TOO_WIDE, TradeBlockReason.LOW_LIQUIDITY}
    assert candidate.trade_eligible is False


def test_update_trade_eligible_when_spread_and_liquidity_are_both_fine():
    watcher = CandidateWatcher()
    candidate = _candidate()
    transition(candidate, CandidateState.WATCHING)
    snapshot = _snapshot_with_conditions(price=10.0, spread_pct=0.1, cumulative_volume=1_000_000)
    watcher.update(candidate, snapshot)
    assert candidate.trade_eligible is True
    assert candidate.block_reasons == []


def test_update_clears_block_reasons_automatically_once_spread_narrows():
    watcher = CandidateWatcher()
    candidate = _candidate()
    transition(candidate, CandidateState.WATCHING)

    watcher.update(candidate, _snapshot_with_conditions(price=10.0, spread_pct=5.0, cumulative_volume=1_000_000))
    assert candidate.trade_eligible is False

    # Nothing needs to explicitly "un-block" the candidate -- block_reasons
    # is recomputed fresh every tick, so a resolved condition clears itself.
    watcher.update(candidate, _snapshot_with_conditions(price=10.0, spread_pct=0.1, cumulative_volume=1_000_000))
    assert candidate.trade_eligible is True
    assert candidate.block_reasons == []


def test_update_still_runs_score_logic_while_trade_blocked():
    # trade_eligible/block_reasons must not short-circuit the rest of
    # update() -- the score/state-transition logic runs regardless, since
    # eligibility and state are orthogonal (see module docstring).
    watcher = CandidateWatcher()
    candidate = _candidate()
    transition(candidate, CandidateState.WATCHING)
    snapshot = _snapshot_with_conditions(price=10.0, spread_pct=5.0, cumulative_volume=1_000_000)
    watcher.update(candidate, snapshot)
    assert candidate.trade_eligible is False
    assert candidate.latest_score is not None  # score computation still happened
    assert candidate.latest_metrics is not None


# -- RVOL baseline lookup (metrics/volume_baseline.py) -- see broad_scanner.py's
# _compute_volume_baseline for where candidate.volume_baseline gets built ---

def test_update_uses_volume_baseline_for_relative_volume():
    from webull_bot.metrics.volume_baseline import VolumeBaseline

    watcher = CandidateWatcher()
    candidate = _candidate()
    candidate.volume_baseline = VolumeBaseline(
        bucket_minutes=5,
        typical_cumulative={("RTH", 0): 500.0},
        typical_bucket_volume={("RTH", 0): 500.0},
    )
    # 2026-08-05 13:30 UTC == 9:30am ET (EDT) -- RTH bucket 0.
    snapshot = _snapshot_with_conditions(price=10.0, spread_pct=0.1, cumulative_volume=1_000.0)
    snapshot.timestamp = datetime(2026, 8, 5, 13, 30)

    watcher.update(candidate, snapshot)

    assert candidate.latest_metrics.relative_volume == 2.0  # 1000 / 500


# -- seed_snapshots splicing (metrics/rolling.seed_history_from_bars) -- see
# broad_scanner.py's _compute_seed_snapshots for where these get built ------

def test_update_splices_seed_snapshots_into_empty_history_only_once():
    from webull_bot.models import MarketSnapshot

    def _seed(minutes_ago, cumulative_volume):
        t = datetime(2026, 8, 10, 13, 30) - timedelta(minutes=minutes_ago)
        return MarketSnapshot(
            symbol="TEST", timestamp=t, last_price=1.0, bid=1.0, ask=1.0, bid_size=0, ask_size=0,
            cumulative_volume=cumulative_volume, vwap=1.0, high_of_day=1.0, low_of_day=1.0, open_price=1.0,
        )

    watcher = CandidateWatcher()
    candidate = _candidate()
    candidate.seed_snapshots = [_seed(4, 400_000.0), _seed(2, 700_000.0)]

    first_snapshot = _snapshot_with_conditions(price=10.0, spread_pct=0.1, cumulative_volume=1_000_000.0)
    first_snapshot.timestamp = datetime(2026, 8, 10, 13, 30)
    watcher.update(candidate, first_snapshot)

    # First tick: history is [seed1, seed2, live] -- vol_5m sees the
    # pre-discovery run-up (1,000,000 - 400,000), not 0.
    assert candidate.latest_metrics.volume_5m == 600_000.0

    second_snapshot = _snapshot_with_conditions(price=10.5, spread_pct=0.1, cumulative_volume=1_050_000.0)
    second_snapshot.timestamp = datetime(2026, 8, 10, 13, 31)
    watcher.update(candidate, second_snapshot)

    # Second tick: seed_snapshots must NOT be re-spliced -- history should
    # be exactly the 3 prior entries plus this one new live tick, not 6.
    assert len(watcher._history["TEST"]) == 4


def test_update_without_seed_snapshots_behaves_exactly_as_before():
    # candidate.seed_snapshots defaults to [] -- must not change behavior
    # for a candidate discovered via a broker with no get_raw_bars support.
    watcher = CandidateWatcher()
    candidate = _candidate()
    assert candidate.seed_snapshots == []

    snapshot = _snapshot_with_conditions(price=10.0, spread_pct=0.1, cumulative_volume=1_000_000.0)
    watcher.update(candidate, snapshot)

    assert candidate.latest_metrics.volume_5m == 0.0
    assert len(watcher._history["TEST"]) == 1


def test_update_relative_volume_defaults_to_neutral_without_baseline():
    # candidate.volume_baseline is None (paper/backtest mode, or a failed
    # discovery-time lookup) -- relative_volume must fall back to its
    # existing neutral default rather than raising or misbehaving.
    watcher = CandidateWatcher()
    candidate = _candidate()
    snapshot = _snapshot_with_conditions(price=10.0, spread_pct=0.1, cumulative_volume=1_000.0)

    watcher.update(candidate, snapshot)

    assert candidate.latest_metrics.relative_volume == 1.0


# -- TICK-derived order flow wiring (2026-08-14) ----------------------------

def test_update_without_get_recent_ticks_fn_leaves_order_flow_unset():
    # No closure at all (the pre-TICK default) -- must behave exactly as
    # before TICK existed, not raise.
    watcher = CandidateWatcher()
    candidate = _candidate()
    snapshot = _snapshot_with_conditions(price=10.0, spread_pct=0.1, cumulative_volume=1_000.0)

    watcher.update(candidate, snapshot)

    assert candidate.latest_metrics.order_flow_imbalance_1m is None
    assert candidate.latest_metrics.buy_volume_1m == 0.0
    assert candidate.latest_metrics.sell_volume_1m == 0.0


def test_update_threads_get_recent_ticks_fn_into_computed_metrics():
    from webull_bot.enums import TradeSide
    from webull_bot.models import TickRecord

    now = datetime.utcnow()
    ticks = [
        TickRecord(symbol="TEST", timestamp=now, price=10.0, volume=80.0, side=TradeSide.BUY),
        TickRecord(symbol="TEST", timestamp=now, price=10.0, volume=20.0, side=TradeSide.SELL),
    ]
    calls = []

    def _get_recent_ticks_fn(symbol):
        calls.append(symbol)
        return ticks

    watcher = CandidateWatcher(get_recent_ticks_fn=_get_recent_ticks_fn)
    candidate = _candidate()
    snapshot = _snapshot_with_conditions(price=10.0, spread_pct=0.1, cumulative_volume=1_000.0)

    watcher.update(candidate, snapshot)

    assert calls == ["TEST"]
    assert candidate.latest_metrics.buy_volume_1m == 80.0
    assert candidate.latest_metrics.sell_volume_1m == 20.0
    assert candidate.latest_metrics.order_flow_imbalance_1m == 0.6  # (80-20)/(80+20)
