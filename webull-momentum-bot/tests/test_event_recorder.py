from datetime import datetime, timedelta

from webull_bot.collection.event_recorder import OUTCOME_WINDOWS, EventRecorder, MomentumEventTracker
from webull_bot.enums import MomentumOutcome
from webull_bot.models import MarketSnapshot, MomentumEvent


def _event(price_at_event=10.0) -> MomentumEvent:
    return MomentumEvent(
        symbol="TEST", detected_at=datetime(2026, 1, 1, 9, 31), trigger_reason="test",
        was_traded=True, score_at_event=80.0, metrics_at_event=None, price_at_event=price_at_event,
    )


def _snapshot(t, price, high=None, vwap=9.5) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="TEST", timestamp=t, last_price=price, bid=price - 0.01, ask=price + 0.01,
        bid_size=100, ask_size=100, cumulative_volume=100_000, vwap=vwap,
        high_of_day=high if high is not None else price, low_of_day=price - 1, open_price=price,
    )


class _SpyRecorder(EventRecorder):
    """Snapshots outcome_label at the moment update() is called, so tests can
    verify the label reflects _finalize()'s result by the time it's persisted
    -- this is exactly the ordering the bug fix addresses."""

    def __init__(self):
        super().__init__()
        self.labels_at_update: list[MomentumOutcome] = []

    def update(self, event_id: int) -> None:
        super().update(event_id)
        self.labels_at_update.append(self.get(event_id).outcome_label)


def test_final_update_call_includes_the_finalized_outcome_label():
    """Regression test: _finalize() used to run AFTER the last recorder.update()
    call for an event, so the CONTINUED/FAILED/CHOPPY label was computed but
    never actually included in what got persisted."""
    recorder = _SpyRecorder()
    tracker = MomentumEventTracker(recorder)
    event = _event(price_at_event=10.0)
    event_id = tracker.register(event)

    t0 = event.detected_at
    # Feed one snapshot per outcome window boundary, well past the last one (15m),
    # with a price high enough to finalize as CONTINUED (>2% at 15m).
    for window_seconds in sorted(OUTCOME_WINDOWS.values()):
        tracker.on_snapshot("TEST", _snapshot(t0 + timedelta(seconds=window_seconds + 1), price=10.5))

    assert event.outcome_label == MomentumOutcome.CONTINUED
    # The label recorded at the moment of the LAST update() call (i.e. what a
    # real DB-backed recorder would have persisted) must already be CONTINUED,
    # not the stale UNKNOWN default from before finalization.
    assert recorder.labels_at_update[-1] == MomentumOutcome.CONTINUED


def test_event_is_dropped_from_pending_once_all_windows_filled():
    recorder = EventRecorder()
    tracker = MomentumEventTracker(recorder)
    event = _event()
    tracker.register(event)

    t0 = event.detected_at
    for window_seconds in sorted(OUTCOME_WINDOWS.values()):
        tracker.on_snapshot("TEST", _snapshot(t0 + timedelta(seconds=window_seconds + 1), price=10.0))

    assert "TEST" not in tracker._pending


def test_failed_outcome_label():
    recorder = EventRecorder()
    tracker = MomentumEventTracker(recorder)
    event = _event(price_at_event=10.0)
    tracker.register(event)

    t0 = event.detected_at
    for window_seconds in sorted(OUTCOME_WINDOWS.values()):
        tracker.on_snapshot("TEST", _snapshot(t0 + timedelta(seconds=window_seconds + 1), price=9.5))  # -5%

    assert event.outcome_label == MomentumOutcome.FAILED


def test_mfe_mae_tracked_across_snapshots():
    recorder = EventRecorder()
    tracker = MomentumEventTracker(recorder)
    event = _event(price_at_event=10.0)
    tracker.register(event)

    t0 = event.detected_at
    tracker.on_snapshot("TEST", _snapshot(t0 + timedelta(seconds=5), price=11.0))  # +10% favorable
    tracker.on_snapshot("TEST", _snapshot(t0 + timedelta(seconds=10), price=9.0))  # -10% adverse

    assert event.max_favorable_excursion_pct == 10.0
    assert event.max_adverse_excursion_pct == 10.0
