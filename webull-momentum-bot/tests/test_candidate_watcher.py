"""
Tests for CandidateWatcher.update_resistance's merge of the running
intraday high with static (volume-profile-derived) resistance levels --
see scanner/candidate_watcher.py's docstring for the "nearest untested
ceiling" rationale. update()'s score/state-transition behavior has
integration coverage via test_trading_loop.py/test_backtest_engine.py;
this file is specifically about the resistance-merge logic that changed,
plus update()'s candidate.last_price bookkeeping (dashboard/app.py's
Price column reads that field).
"""
from datetime import datetime

from webull_bot.enums import CandidateState
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
