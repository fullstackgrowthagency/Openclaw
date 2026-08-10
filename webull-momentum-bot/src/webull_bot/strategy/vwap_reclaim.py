"""
VWAP Reclaim Continuation strategy: catches a candidate that dipped
meaningfully below VWAP and is now reclaiming it with fresh volume -- the
"second leg" of a move that resistance-breakout and volume-ignition
entries both miss (ignition requires being above VWAP already; breakout
requires a specific price level). See docs/ARCHITECTURE.md's "Entry
strategies" section for how this fits alongside the others added
alongside it.

Needs a small amount of per-symbol state (was this candidate meaningfully
below VWAP recently?) since a single snapshot's distance_from_vwap_pct > 0
alone can't distinguish "just reclaimed" from "has been above VWAP for
hours" -- kept internal to this strategy instance (a plain dict keyed by
symbol), not on the shared Candidate object, specifically so multiple
stateful strategies (this one, IgnitionPullbackStrategy,
BreakoutPullbackStrategy) can track their own phases independently without
clobbering each other's state on the same candidate.

Stop is VWAP-anchored rather than a flat %, since VWAP is the level this
entry is explicitly betting will now hold as support.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..enums import CandidateState, SignalAction
from ..interfaces.strategy import Strategy
from ..models import Candidate, MarketSnapshot, Signal


@dataclass
class VWAPReclaimConfig:
    below_vwap_threshold_pct: float = -0.3   # must have dipped at least this far below VWAP to arm the "was below" flag
    reclaim_buffer_pct: float = 0.1          # must clear VWAP by this much (not just touch it) to trigger entry
    min_volume_acceleration: float = 1.5
    max_spread_pct: float = 2.0
    stop_buffer_pct: float = 0.5             # stop placed this % below VWAP itself
    profit_target_r_multiple: float = 2.0


class VWAPReclaimStrategy(Strategy):
    name = "vwap_reclaim"
    version = "v1"

    def __init__(self, config: Optional[VWAPReclaimConfig] = None):
        self.config = config or VWAPReclaimConfig()
        self._was_below_vwap: dict[str, bool] = {}

    def on_snapshot(self, candidate: Candidate, snapshot: MarketSnapshot) -> Optional[Signal]:
        if candidate.state != CandidateState.ARMED:
            return None
        metrics = candidate.latest_metrics
        if metrics is None:
            return None

        was_below = self._was_below_vwap.get(candidate.symbol, False)

        if metrics.distance_from_vwap_pct <= self.config.below_vwap_threshold_pct:
            self._was_below_vwap[candidate.symbol] = True
            return None

        if not was_below:
            return None  # never dipped meaningfully below VWAP recently -- nothing to "reclaim"

        if metrics.distance_from_vwap_pct < self.config.reclaim_buffer_pct:
            return None  # still below/at VWAP, not a confirmed reclaim yet
        if metrics.volume_accel_1m_3m < self.config.min_volume_acceleration:
            return None
        if metrics.spread_pct > self.config.max_spread_pct:
            return None

        entry_price = snapshot.last_price
        stop_price = metrics.vwap * (1 - self.config.stop_buffer_pct / 100.0)
        risk_per_share = entry_price - stop_price
        if risk_per_share <= 0:
            return None
        target_price = entry_price + risk_per_share * self.config.profit_target_r_multiple

        self._was_below_vwap[candidate.symbol] = False  # reset -- a fresh dip must occur before this can fire again
        return Signal(
            symbol=candidate.symbol,
            action=SignalAction.ENTER_LONG,
            generated_at=snapshot.timestamp,
            strategy_name=self.name,
            strategy_version=self.version,
            reference_price=entry_price,
            suggested_stop=stop_price,
            suggested_target=target_price,
            score_at_signal=candidate.latest_score.score if candidate.latest_score else None,
            metadata={"vwap": metrics.vwap},
        )
