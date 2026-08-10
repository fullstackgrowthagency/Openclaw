"""
Refined Breakout strategy: a stricter variant of MomentumBreakoutStrategy
that only fires in a bounded window right at the breakout -- price must be
between the resistance level and resistance_level * (1 + max_breakout_extension_pct/100)
(3% above resistance by default, per explicit user spec). The plain
breakout strategy has no upper bound at all, so it will happily "confirm"
a breakout of a level price already ran far past hours ago; this variant
is reserved for a genuinely fresh, in-progress break, not a stale,
already-extended one -- see docs/ARCHITECTURE.md's "Entry strategies"
section for how this fits alongside the other new strategies (in
particular Volume Ignition, which is what actually catches a candidate
whose resistance is too far away to be a useful reference at all).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..enums import CandidateState, SignalAction
from ..interfaces.strategy import Strategy
from ..models import Candidate, MarketSnapshot, Signal


@dataclass
class RefinedBreakoutConfig:
    max_breakout_extension_pct: float = 3.0  # entry only valid up to this % above resistance -- the "3% buffer" spec
    min_volume_acceleration: float = 1.3
    max_spread_pct: float = 2.0
    initial_stop_pct: float = 3.0
    profit_target_r_multiple: float = 2.0


class RefinedBreakoutStrategy(Strategy):
    name = "refined_breakout"
    version = "v1"

    def __init__(self, config: Optional[RefinedBreakoutConfig] = None):
        self.config = config or RefinedBreakoutConfig()

    def on_snapshot(self, candidate: Candidate, snapshot: MarketSnapshot) -> Optional[Signal]:
        if candidate.state != CandidateState.ARMED:
            return None
        if candidate.resistance_level is None or candidate.latest_metrics is None:
            return None

        resistance = candidate.resistance_level
        max_valid_price = resistance * (1 + self.config.max_breakout_extension_pct / 100.0)
        if not (resistance <= snapshot.last_price <= max_valid_price):
            return None

        if candidate.latest_metrics.volume_accel_1m_3m < self.config.min_volume_acceleration:
            return None
        if candidate.latest_metrics.spread_pct > self.config.max_spread_pct:
            return None

        entry_price = snapshot.last_price
        stop_price = min(resistance, entry_price * (1 - self.config.initial_stop_pct / 100.0))
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
            metadata={"resistance_level": resistance, "max_valid_price": max_valid_price},
        )
