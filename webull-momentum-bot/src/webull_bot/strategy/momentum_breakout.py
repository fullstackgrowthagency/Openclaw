"""
Momentum Breakout strategy: enter when an ARMED candidate breaks its
tracked resistance level with expanding volume and an acceptable spread.
This is the simpler of the two initial entry styles from the project
outline -- see breakout_pullback.py for the second, which is expected to
need more testing since it targets a less exhausted entry point.

target_price is computed from reward_risk_ratio_fn(), not a hardcoded
per-strategy multiple -- see main.py's build_trading_loop, which wires
this to the live RiskEngine.config.min_risk_reward_ratio (the same value
adjustable from the dashboard's Settings panel) so every strategy's target
moves together when that setting changes. Defaults to a fixed 2.0 when not
supplied (tests, standalone use).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from ..enums import CandidateState, SignalAction
from ..interfaces.strategy import Strategy
from ..models import Candidate, MarketSnapshot, Signal


@dataclass
class MomentumBreakoutConfig:
    breakout_buffer_pct: float = 0.1       # price must clear resistance by this much to count
    min_volume_acceleration: float = 1.3   # candidate.latest_metrics.volume_accel_1m_3m must exceed this
    max_spread_pct: float = 2.0
    initial_stop_pct: float = 3.0          # stop placed this % below entry if no better level is known


class MomentumBreakoutStrategy(Strategy):
    name = "momentum_breakout"
    version = "v1"

    def __init__(self, config: Optional[MomentumBreakoutConfig] = None, *, reward_risk_ratio_fn: Callable[[], float] = lambda: 2.0):
        self.config = config or MomentumBreakoutConfig()
        self._reward_risk_ratio_fn = reward_risk_ratio_fn

    def on_snapshot(self, candidate: Candidate, snapshot: MarketSnapshot) -> Optional[Signal]:
        if candidate.state != CandidateState.ARMED:
            return None
        if candidate.resistance_level is None or candidate.latest_metrics is None:
            return None

        breakout_level = candidate.resistance_level * (1 + self.config.breakout_buffer_pct / 100.0)
        if snapshot.last_price < breakout_level:
            return None

        if candidate.latest_metrics.volume_accel_1m_3m < self.config.min_volume_acceleration:
            return None

        if candidate.latest_metrics.spread_pct > self.config.max_spread_pct:
            return None

        entry_price = snapshot.last_price
        stop_price = min(candidate.resistance_level, entry_price * (1 - self.config.initial_stop_pct / 100.0))
        risk_per_share = entry_price - stop_price
        target_price = entry_price + risk_per_share * self._reward_risk_ratio_fn()

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
            metadata={"resistance_level": candidate.resistance_level},
        )
