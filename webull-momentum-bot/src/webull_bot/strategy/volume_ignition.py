"""
Volume Ignition strategy: catches a candidate "about to take off" from
current activity alone, with no resistance-level requirement at all --
the direct fix for resistance-only entries missing candidates whose
nearest resistance is too far away to be a useful near-term reference
(see docs/ARCHITECTURE.md's "Entry strategies" section for the full
reasoning behind this and the other strategies added alongside it).

Fires when EITHER share-volume acceleration (volume_accel_1m_3m) OR
float-turnover velocity (float_velocity_5m -- % of float traded in the
last 5 minutes) crosses its threshold -- these catch different things:
a heavily-diluted name can show strong float velocity on volume that
wouldn't look extreme in raw acceleration terms, and vice versa for a
truly thin float where a modest share count is already a large chunk of
it. Either alone is treated as a real ignition signal, not requiring both.

A volume spike alone is ambiguous -- it's just as consistent with a dump
as a breakout -- so two more conditions are required to confirm this is
upward momentum, not distribution: price must actually be rising
(price_velocity_1m > 0) and trading above VWAP (the standard momentum-long
filter against buying into a fade). Because there's no resistance level to
anchor a target to, `suggested_target` is deliberately left None here --
the existing trailing-stop/VWAP-failure/time-limit exits in
PositionManager are what let a winner run instead of capping it at a
fixed R-multiple.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..enums import CandidateState, SignalAction
from ..interfaces.strategy import Strategy
from ..models import Candidate, MarketSnapshot, Signal


@dataclass
class VolumeIgnitionConfig:
    min_volume_acceleration: float = 3.0     # candidate.latest_metrics.volume_accel_1m_3m must exceed this...
    min_float_velocity_5m: Optional[float] = 0.03  # ...OR this (3% of float traded in 5 min) -- either alone qualifies
    max_spread_pct: float = 2.0
    initial_stop_pct: float = 2.5            # tighter than the breakout strategies' 3% -- no resistance/pullback level to lean on instead


class VolumeIgnitionStrategy(Strategy):
    name = "volume_ignition"
    version = "v1"

    def __init__(self, config: Optional[VolumeIgnitionConfig] = None):
        self.config = config or VolumeIgnitionConfig()

    def on_snapshot(self, candidate: Candidate, snapshot: MarketSnapshot) -> Optional[Signal]:
        if candidate.state != CandidateState.ARMED:
            return None
        metrics = candidate.latest_metrics
        if metrics is None:
            return None

        volume_igniting = metrics.volume_accel_1m_3m >= self.config.min_volume_acceleration
        float_igniting = (
            self.config.min_float_velocity_5m is not None
            and metrics.float_velocity_5m >= self.config.min_float_velocity_5m
        )
        if not (volume_igniting or float_igniting):
            return None

        if metrics.price_velocity_1m <= 0:
            return None  # volume spiking while price falls is distribution, not ignition
        if metrics.distance_from_vwap_pct <= 0:
            return None  # standard momentum-long filter: only above VWAP
        if metrics.spread_pct > self.config.max_spread_pct:
            return None

        entry_price = snapshot.last_price
        stop_price = entry_price * (1 - self.config.initial_stop_pct / 100.0)

        return Signal(
            symbol=candidate.symbol,
            action=SignalAction.ENTER_LONG,
            generated_at=snapshot.timestamp,
            strategy_name=self.name,
            strategy_version=self.version,
            reference_price=entry_price,
            suggested_stop=stop_price,
            suggested_target=None,  # let the trailing stop / VWAP-failure exit run the winner
            score_at_signal=candidate.latest_score.score if candidate.latest_score else None,
            metadata={
                "volume_accel_1m_3m": metrics.volume_accel_1m_3m,
                "float_velocity_5m": metrics.float_velocity_5m,
                "trigger": "volume" if volume_igniting else "float_velocity",
            },
        )
