"""
Records momentum events -- traded AND non-traded -- and tracks their
forward-looking outcomes so the collected data can eventually be analyzed
to improve the Momentum Ignition Score formula and entry strategies.

Usage pattern:
    tracker = MomentumEventTracker(recorder)
    tracker.register(event)                 # when a notable momentum event fires
    tracker.on_snapshot(symbol, snapshot)    # called on every subsequent snapshot for that symbol
    # once outcome_15m is filled, tracker.finalize(event) labels it continued/failed/choppy
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

from ..enums import MomentumOutcome
from ..metrics.rolling import _reference_price_at
from ..models import MarketSnapshot, MomentumEvent, TickRecord

# name -> seconds after detection
OUTCOME_WINDOWS: dict[str, int] = {
    "outcome_30s": 30,
    "outcome_1m": 60,
    "outcome_3m": 180,
    "outcome_5m": 300,
    "outcome_10m": 600,
    "outcome_15m": 900,
}

# Real-Time Momentum Qualification Layer (2026-08-17): finer-grained than
# OUTCOME_WINDOWS above -- a momentum-qualification-specific trigger can
# fail or succeed within seconds, well before outcome_30s fills in. Kept as
# a separate dict (not merged into OUTCOME_WINDOWS) since these resolve
# their price from tick data when available (see
# MomentumEventTracker._fine_snapshot_outcome) rather than the plain
# current-snapshot price the coarser windows use -- the on_snapshot poll
# cadence (as slow as every 5s) alone isn't precise enough for a 5s window.
FINE_OUTCOME_WINDOWS: dict[str, float] = {
    "outcome_5s": 5.0,
    "outcome_10s": 10.0,
    "outcome_15s": 15.0,
}


class EventRecorder:
    """Persistence boundary. Kept separate from MomentumEventTracker's
    in-memory tracking logic so tests can use an in-memory fake here."""

    def __init__(self):
        self._events: dict[int, MomentumEvent] = {}
        self._next_id = 1

    def save(self, event: MomentumEvent) -> int:
        event_id = self._next_id
        self._events[event_id] = event
        self._next_id += 1
        return event_id

    def update(self, event_id: int) -> None:
        # In-memory store already holds the mutated object; a real
        # implementation would UPSERT into MomentumEventRecord here via
        # db/session.py. Left as a hook so tests don't need a live DB.
        pass

    def get(self, event_id: int) -> MomentumEvent:
        return self._events[event_id]


@dataclass
class _PendingEvent:
    event_id: int
    event: MomentumEvent
    filled_windows: set[str] = field(default_factory=set)


class MomentumEventTracker:
    def __init__(
        self, recorder: EventRecorder, *,
        get_recent_ticks_fn: Optional[Callable[[str], list[TickRecord]]] = None,
    ):
        self.recorder = recorder
        # Real-Time Momentum Qualification Layer (2026-08-17): optional
        # broker capability closure, same getattr-gated-at-the-call-site
        # pattern as CandidateWatcher's own get_recent_ticks_fn -- lets
        # FINE_OUTCOME_WINDOWS resolve from real tick timestamps instead of
        # this tracker's own on_snapshot cadence. None (the default) simply
        # falls back to the current snapshot's price for those windows too
        # -- today's behavior for PaperBrokerClient/backtests, unaffected.
        self._get_recent_ticks_fn = get_recent_ticks_fn
        self._pending: dict[str, list[_PendingEvent]] = {}

    def bind_recent_ticks_fn(self, get_recent_ticks_fn: Callable[[str], list[TickRecord]]) -> None:
        """Lets a caller that must construct this tracker before a broker
        exists yet (see scripts/run_dashboard.py's _build_loop_for_user,
        which builds the tracker up front so DBBackedEventRecorder can be
        wired in, well before main.py's build_trading_loop creates the
        broker) supply the tick-lookup capability later. No-op if one was
        already given to __init__ -- never overwrites an explicit value
        with a broker-derived default."""
        if self._get_recent_ticks_fn is None:
            self._get_recent_ticks_fn = get_recent_ticks_fn

    def register(self, event: MomentumEvent) -> int:
        event_id = self.recorder.save(event)
        self._pending.setdefault(event.symbol, []).append(_PendingEvent(event_id=event_id, event=event))
        return event_id

    def _snapshot_outcome(self, event: MomentumEvent, snapshot: MarketSnapshot) -> dict:
        pct_change = (snapshot.last_price - event.price_at_event) / event.price_at_event * 100.0
        return {
            "price": snapshot.last_price,
            "pct_change": pct_change,
            "high_of_day": snapshot.high_of_day,
            "vwap": snapshot.vwap,
            "timestamp": snapshot.timestamp.isoformat(),
        }

    def _fine_snapshot_outcome(
        self, event: MomentumEvent, target_time: datetime, snapshot: MarketSnapshot,
        ticks: Optional[list[TickRecord]],
    ) -> dict:
        """FINE_OUTCOME_WINDOWS' outcome builder -- resolves the price at
        exactly `target_time` from already-buffered tick data (reusing
        metrics/rolling.py's own tick-lookup helper) when ticks were
        supplied, falling back to the current snapshot's price otherwise
        (no ticks available, or nothing landed close enough to
        target_time yet). mfe_pct/mae_pct reuse this tracker's own
        already-running max_favorable_excursion_pct/max_adverse_excursion_pct
        (updated just above in on_snapshot, off the current snapshot) --
        not a separate computation."""
        price = _reference_price_at(ticks, target_time) if ticks else None
        if price is None:
            price = snapshot.last_price
        pct_change = (price - event.price_at_event) / event.price_at_event * 100.0
        return {
            "price": price,
            "pct_change": pct_change,
            "mfe_pct": event.max_favorable_excursion_pct,
            "mae_pct": event.max_adverse_excursion_pct,
            "timestamp": target_time.isoformat(),
        }

    def on_snapshot(self, symbol: str, snapshot: MarketSnapshot) -> None:
        pending_list = self._pending.get(symbol)
        if not pending_list:
            return

        ticks = self._get_recent_ticks_fn(symbol) if self._get_recent_ticks_fn is not None else None

        still_pending: list[_PendingEvent] = []
        for pending in pending_list:
            event = pending.event
            elapsed = (snapshot.timestamp - event.detected_at).total_seconds()

            favorable = (snapshot.last_price - event.price_at_event) / event.price_at_event * 100.0
            adverse = -favorable
            event.max_favorable_excursion_pct = max(event.max_favorable_excursion_pct or 0.0, favorable)
            event.max_adverse_excursion_pct = max(event.max_adverse_excursion_pct or 0.0, adverse)

            if snapshot.high_of_day > event.price_at_event and not event.hod_broken:
                event.hod_broken = True
            if snapshot.vwap and snapshot.last_price < snapshot.vwap:
                event.vwap_failed = True

            for field_name, window_seconds in OUTCOME_WINDOWS.items():
                if field_name in pending.filled_windows:
                    continue
                if elapsed >= window_seconds:
                    setattr(event, field_name, self._snapshot_outcome(event, snapshot))
                    pending.filled_windows.add(field_name)

            for field_name, window_seconds in FINE_OUTCOME_WINDOWS.items():
                if field_name in pending.filled_windows:
                    continue
                if elapsed >= window_seconds:
                    target_time = event.detected_at + timedelta(seconds=window_seconds)
                    setattr(event, field_name, self._fine_snapshot_outcome(event, target_time, snapshot, ticks))
                    pending.filled_windows.add(field_name)

            all_windows_filled = len(pending.filled_windows) >= (len(OUTCOME_WINDOWS) + len(FINE_OUTCOME_WINDOWS))
            if all_windows_filled:
                # Must run BEFORE recorder.update() below, so the final
                # CONTINUED/FAILED/CHOPPY label is included in what gets
                # persisted -- previously this ran after the last update()
                # call and the label change was silently never saved.
                self._finalize(event)
            self.recorder.update(pending.event_id)

            if not all_windows_filled:
                still_pending.append(pending)

        if still_pending:
            self._pending[symbol] = still_pending
        else:
            self._pending.pop(symbol, None)

    def _finalize(self, event: MomentumEvent) -> None:
        outcome_15m = event.outcome_15m
        if outcome_15m is None:
            event.outcome_label = MomentumOutcome.UNKNOWN
            return
        pct_change = outcome_15m["pct_change"]
        mfe = event.max_favorable_excursion_pct or 0.0
        if pct_change > 2.0:
            event.outcome_label = MomentumOutcome.CONTINUED
        elif pct_change < -1.0:
            event.outcome_label = MomentumOutcome.FAILED
        elif mfe > 5.0 and pct_change < mfe * 0.3:
            event.outcome_label = MomentumOutcome.CHOPPY
        else:
            event.outcome_label = MomentumOutcome.CHOPPY

    def expire_stale(self, now: datetime, max_age: timedelta = timedelta(hours=2)) -> None:
        """Drop tracking for events that never received enough snapshots to
        complete (e.g. the symbol halted or went illiquid) so memory doesn't grow unbounded."""
        for symbol in list(self._pending.keys()):
            self._pending[symbol] = [
                p for p in self._pending[symbol] if now - p.event.detected_at < max_age
            ]
            if not self._pending[symbol]:
                del self._pending[symbol]
