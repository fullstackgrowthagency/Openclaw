"""
Opening Range Breakout (ORB) strategy: a time-based breakout reference --
the high of the first opening_range_minutes of the session
(candidate.opening_range_high, computed once at discovery, see
metrics/opening_range.py) -- completely independent of the volume-profile
resistance levels used by the other breakout strategies. One of the most
standard day-trading patterns for this bot's target universe, and it
catches candidates the resistance-based strategies structurally can't:
a symbol with no meaningful historical volume-profile cluster (e.g. a
recent IPO, or one that just hasn't traded enough to build one) still has
an opening range every single day. See docs/ARCHITECTURE.md's "Entry
strategies" section.

opening_range_high is None whenever bars didn't cover market open (e.g.
discovered well after the open, or no get_raw_bars capability) -- this
strategy simply never fires for that candidate then, same fail-soft
contract as the rest of the discovery-time enrichment it's built on.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..enums import CandidateState, SignalAction
from ..interfaces.strategy import Strategy
from ..models import Candidate, MarketSnapshot, Signal


@dataclass
class OpeningRangeBreakoutConfig:
    breakout_buffer_pct: float = 0.1
    min_volume_acceleration: float = 1.3
    max_spread_pct: float = 2.0
    initial_stop_pct: float = 3.0
    profit_target_r_multiple: float = 2.0


class OpeningRangeBreakoutStrategy(Strategy):
    name = "opening_range_breakout"
    version = "v1"

    def __init__(self, config: Optional[OpeningRangeBreakoutConfig] = None):
        self.config = config or OpeningRangeBreakoutConfig()

    def on_snapshot(self, candidate: Candidate, snapshot: MarketSnapshot) -> Optional[Signal]:
        if candidate.state != CandidateState.ARMED:
            return None
        if candidate.opening_range_high is None or candidate.latest_metrics is None:
            return None

        breakout_level = candidate.opening_range_high * (1 + self.config.breakout_buffer_pct / 100.0)
        if snapshot.last_price < breakout_level:
            return None

        if candidate.latest_metrics.volume_accel_1m_3m < self.config.min_volume_acceleration:
            return None
        if candidate.latest_metrics.spread_pct > self.config.max_spread_pct:
            return None

        entry_price = snapshot.last_price
        stop_price = min(candidate.opening_range_high, entry_price * (1 - self.config.initial_stop_pct / 100.0))
        risk_per_share = entry_price - stop_price
        if risk_per_share <= 0:
            return None
        target_price = entry_price + risk_per_share * self.config.profit_target_r_multiple

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
            metadata={"opening_range_high": candidate.opening_range_high},
        )
