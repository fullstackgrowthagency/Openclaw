"""
Breakout-Pullback strategy: wait for the initial resistance breakout, then
require a controlled pullback on decreasing volume, and only enter once
price reclaims the pullback high with momentum returning. Per the project
outline this should be tested heavily against the plain momentum-breakout
strategy since it targets entries further from short-term exhaustion.

Per-symbol pullback tracking is kept in `candidate.breakout_price` /
`candidate.pullback_low` (on the shared Candidate object) plus a small
internal phase map here, since a single strategy instance is shared across
many candidates.

target_price is computed from reward_risk_ratio_fn(), not a hardcoded
per-strategy multiple -- see main.py's build_trading_loop, which wires
this to the live RiskEngine.config.min_risk_reward_ratio (the same value
adjustable from the dashboard's Settings panel) so every strategy's target
moves together when that setting changes. Defaults to a fixed 2.0 when not
supplied (tests, standalone use).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from ..enums import CandidateState, SignalAction
from ..interfaces.strategy import Strategy
from ..models import Candidate, MarketSnapshot, Signal


class _Phase(str, Enum):
    AWAITING_BREAKOUT = "awaiting_breakout"
    PULLBACK_FORMING = "pullback_forming"
    READY_TO_ENTER = "ready_to_enter"


@dataclass
class BreakoutPullbackConfig:
    breakout_buffer_pct: float = 0.1
    max_pullback_retrace_pct: float = 50.0   # pullback must not retrace more than this % of the breakout move
    min_pullback_bars: int = 2               # require at least this many snapshots of pulling back
    pullback_volume_decline_required: bool = True
    reclaim_buffer_pct: float = 0.05         # price must clear pullback high by this much to trigger entry
    max_spread_pct: float = 2.0
    initial_stop_buffer_pct: float = 0.5     # stop placed this % below the pullback low


class BreakoutPullbackStrategy(Strategy):
    name = "breakout_pullback"
    version = "v1"

    def __init__(self, config: Optional[BreakoutPullbackConfig] = None, *, reward_risk_ratio_fn: Callable[[], float] = lambda: 2.0):
        self.config = config or BreakoutPullbackConfig()
        self._reward_risk_ratio_fn = reward_risk_ratio_fn
        self._phase: dict[str, _Phase] = {}
        self._prior_volume_rate: dict[str, float] = {}
        # Real bug fixed 2026-08-12: min_pullback_bars was declared and
        # documented ("require at least this many snapshots of pulling
        # back") but never actually read anywhere -- the PULLBACK_FORMING
        # -> READY_TO_ENTER transition below had no bar-count check at
        # all, so a pullback lasting exactly one tick (price dips once,
        # reverses up on the very next snapshot) satisfied it just as
        # readily as a genuine multi-bar pullback, defeating the quality
        # filter this config field exists to be.
        self._pullback_bars: dict[str, int] = {}

    def on_snapshot(self, candidate: Candidate, snapshot: MarketSnapshot) -> Optional[Signal]:
        if candidate.state != CandidateState.ARMED or candidate.resistance_level is None:
            return None

        phase = self._phase.get(candidate.symbol, _Phase.AWAITING_BREAKOUT)
        metrics = candidate.latest_metrics

        if phase == _Phase.AWAITING_BREAKOUT:
            breakout_level = candidate.resistance_level * (1 + self.config.breakout_buffer_pct / 100.0)
            if snapshot.last_price >= breakout_level:
                candidate.breakout_price = snapshot.last_price
                candidate.pullback_low = snapshot.last_price
                self._phase[candidate.symbol] = _Phase.PULLBACK_FORMING
                self._pullback_bars[candidate.symbol] = 0
            return None

        if phase == _Phase.PULLBACK_FORMING:
            assert candidate.breakout_price is not None
            self._pullback_bars[candidate.symbol] = self._pullback_bars.get(candidate.symbol, 0) + 1
            if snapshot.last_price < (candidate.pullback_low or candidate.breakout_price):
                candidate.pullback_low = snapshot.last_price

            retrace_pct = (
                (candidate.breakout_price - snapshot.last_price)
                / (candidate.breakout_price - candidate.resistance_level)
                * 100.0
                if candidate.breakout_price > candidate.resistance_level
                else 0.0
            )
            if retrace_pct > self.config.max_pullback_retrace_pct:
                # Pulled back too far -- invalidate and go back to watching for a fresh breakout.
                self._phase[candidate.symbol] = _Phase.AWAITING_BREAKOUT
                candidate.breakout_price = None
                candidate.pullback_low = None
                self._pullback_bars.pop(candidate.symbol, None)
                return None

            if (
                metrics is not None
                and snapshot.last_price > (candidate.pullback_low or 0)
                and self._pullback_bars[candidate.symbol] >= self.config.min_pullback_bars
            ):
                # Price is turning back up off the pullback low -- check if momentum
                # is returning (volume accelerating again) before arming for entry.
                if not self.config.pullback_volume_decline_required or metrics.volume_accel_1m_3m > 1.0:
                    self._phase[candidate.symbol] = _Phase.READY_TO_ENTER
            return None

        if phase == _Phase.READY_TO_ENTER:
            assert candidate.pullback_low is not None
            reclaim_level = candidate.pullback_low * (1 + self.config.reclaim_buffer_pct / 100.0)
            if snapshot.last_price < reclaim_level:
                return None
            if metrics is not None and metrics.spread_pct > self.config.max_spread_pct:
                return None

            entry_price = snapshot.last_price
            stop_price = candidate.pullback_low * (1 - self.config.initial_stop_buffer_pct / 100.0)
            risk_per_share = entry_price - stop_price
            if risk_per_share <= 0:
                return None
            target_price = entry_price + risk_per_share * self._reward_risk_ratio_fn()

            signal = Signal(
                symbol=candidate.symbol,
                action=SignalAction.ENTER_LONG,
                generated_at=snapshot.timestamp,
                strategy_name=self.name,
                strategy_version=self.version,
                reference_price=entry_price,
                suggested_stop=stop_price,
                suggested_target=target_price,
                score_at_signal=candidate.latest_score.score if candidate.latest_score else None,
                metadata={
                    "breakout_price": candidate.breakout_price,
                    "pullback_low": candidate.pullback_low,
                },
            )
            # Reset phase tracking; a fresh breakout must be detected before this can fire again.
            self._phase[candidate.symbol] = _Phase.AWAITING_BREAKOUT
            candidate.breakout_price = None
            candidate.pullback_low = None
            self._pullback_bars.pop(candidate.symbol, None)
            return signal

        return None
