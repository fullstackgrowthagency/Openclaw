"""
Ignition-Pullback strategy: the same "confirm it's continuing, not faking
out" pattern as BreakoutPullbackStrategy (wait for a pullback on declining
volume, then enter on reclaim with momentum returning), but anchored to a
Volume Ignition move instead of a resistance breakout -- so it works on
any candidate seeing a real volume+price surge, not just ones near a
known resistance level. See docs/ARCHITECTURE.md's "Entry strategies"
section.

Per-symbol phase/price tracking is kept entirely internal to this strategy
instance (plain dicts keyed by symbol), NOT on the shared Candidate object
the way BreakoutPullbackStrategy uses candidate.breakout_price/pullback_low
-- with multiple pullback-style strategies now registered together, two
strategies sharing state on the same Candidate fields would silently
clobber each other every tick one of them is asked but the other already
mutated those fields. Keeping this strategy's state private avoids that
collision entirely.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from ..enums import CandidateState, SignalAction
from ..interfaces.strategy import Strategy
from ..models import Candidate, MarketSnapshot, Signal


class _Phase(str, Enum):
    AWAITING_IGNITION = "awaiting_ignition"
    PULLBACK_FORMING = "pullback_forming"
    READY_TO_ENTER = "ready_to_enter"


@dataclass
class IgnitionPullbackConfig:
    # Ignition detection -- same shape as VolumeIgnitionConfig, kept
    # separate (not shared) so each strategy's thresholds can be tuned
    # independently.
    min_volume_acceleration: float = 3.0
    min_float_velocity_5m: Optional[float] = 0.03
    max_pullback_retrace_pct: float = 50.0   # pullback must not retrace more than this % of the ignition move
    pullback_volume_decline_required: bool = True
    reclaim_buffer_pct: float = 0.1
    max_spread_pct: float = 2.0
    initial_stop_buffer_pct: float = 0.5     # stop placed this % below the pullback low
    profit_target_r_multiple: float = 2.0


class IgnitionPullbackStrategy(Strategy):
    name = "ignition_pullback"
    version = "v1"

    def __init__(self, config: Optional[IgnitionPullbackConfig] = None):
        self.config = config or IgnitionPullbackConfig()
        self._phase: dict[str, _Phase] = {}
        self._ignition_price: dict[str, float] = {}
        self._pullback_low: dict[str, float] = {}

    def _is_igniting(self, metrics) -> bool:
        volume_igniting = metrics.volume_accel_1m_3m >= self.config.min_volume_acceleration
        float_igniting = (
            self.config.min_float_velocity_5m is not None
            and metrics.float_velocity_5m >= self.config.min_float_velocity_5m
        )
        return (
            (volume_igniting or float_igniting)
            and metrics.price_velocity_1m > 0
            and metrics.distance_from_vwap_pct > 0
        )

    def on_snapshot(self, candidate: Candidate, snapshot: MarketSnapshot) -> Optional[Signal]:
        if candidate.state != CandidateState.ARMED:
            return None
        metrics = candidate.latest_metrics
        if metrics is None:
            return None

        symbol = candidate.symbol
        phase = self._phase.get(symbol, _Phase.AWAITING_IGNITION)

        if phase == _Phase.AWAITING_IGNITION:
            if self._is_igniting(metrics):
                self._ignition_price[symbol] = snapshot.last_price
                self._pullback_low[symbol] = snapshot.last_price
                self._phase[symbol] = _Phase.PULLBACK_FORMING
            return None

        if phase == _Phase.PULLBACK_FORMING:
            ignition_price = self._ignition_price[symbol]
            pullback_low = self._pullback_low.get(symbol, ignition_price)
            if snapshot.last_price < pullback_low:
                pullback_low = snapshot.last_price
                self._pullback_low[symbol] = pullback_low

            retrace_pct = (ignition_price - pullback_low) / ignition_price * 100.0 if ignition_price else 0.0
            if retrace_pct > self.config.max_pullback_retrace_pct:
                # Pulled back too far -- invalidate and wait for a fresh ignition.
                self._phase[symbol] = _Phase.AWAITING_IGNITION
                self._ignition_price.pop(symbol, None)
                self._pullback_low.pop(symbol, None)
                return None

            if snapshot.last_price > pullback_low:
                if not self.config.pullback_volume_decline_required or metrics.volume_accel_1m_3m > 1.0:
                    self._phase[symbol] = _Phase.READY_TO_ENTER
            return None

        if phase == _Phase.READY_TO_ENTER:
            pullback_low = self._pullback_low[symbol]
            reclaim_level = pullback_low * (1 + self.config.reclaim_buffer_pct / 100.0)
            if snapshot.last_price < reclaim_level:
                return None
            if metrics.spread_pct > self.config.max_spread_pct:
                return None

            entry_price = snapshot.last_price
            stop_price = pullback_low * (1 - self.config.initial_stop_buffer_pct / 100.0)
            risk_per_share = entry_price - stop_price
            if risk_per_share <= 0:
                return None
            target_price = entry_price + risk_per_share * self.config.profit_target_r_multiple

            signal = Signal(
                symbol=symbol,
                action=SignalAction.ENTER_LONG,
                generated_at=snapshot.timestamp,
                strategy_name=self.name,
                strategy_version=self.version,
                reference_price=entry_price,
                suggested_stop=stop_price,
                suggested_target=target_price,
                score_at_signal=candidate.latest_score.score if candidate.latest_score else None,
                metadata={"ignition_price": self._ignition_price.get(symbol), "pullback_low": pullback_low},
            )
            # Reset phase tracking; a fresh ignition must be detected before this can fire again.
            self._phase[symbol] = _Phase.AWAITING_IGNITION
            self._ignition_price.pop(symbol, None)
            self._pullback_low.pop(symbol, None)
            return signal

        return None
