"""
Volatility Contraction Breakout ("flag/pennant") strategy: price tightens
into a narrow range after an initial move, then expands out of it with
volume -- a classic continuation setup distinct from all the other
strategies (none of them look at whether price has gotten *quiet* before
moving again). See docs/ARCHITECTURE.md's "Entry strategies" section.

Contraction is measured as the ratio of a tight recent window's price
range to a broader context window's range (metrics.price_range_pct_3m /
price_range_pct_15m -- see metrics/calculations.py's price_range_pct and
metrics/rolling.py's compute_metrics, which already slices both windows
for other metrics, so nothing new is fetched here). A low ratio means the
last 3 minutes have been much quieter than the last 15 -- a real
contraction, not just a generally quiet/dead name (which would show a low
range on BOTH windows, not a low ratio between them).

This is a coarser signal than genuine swing-high tracking (the classic
version of this pattern watches a specific consolidation high) --
deliberately simplified here to "was tight, now expanding with volume and
rising price" using only already-computed per-tick metrics, rather than
adding new stateful range/swing tracking on top of an already large batch
of new strategies. Worth revisiting with real consolidation-high tracking
if this version underperforms in practice.

target_price is computed from reward_risk_ratio_fn(), not a hardcoded
per-strategy multiple -- see main.py's build_trading_loop, which wires
this to the live RiskEngine.config.min_risk_reward_ratio (the same value
adjustable from the dashboard's Settings panel) so every strategy's target
moves together when that setting changes. Defaults to a fixed 2.0 when not
supplied (tests, standalone use).

stop_price is likewise computed from stop_loss_pct_fn(), wired to the live
RiskEngine.config.stop_loss_pct -- see that field's docstring. Defaults to
a fixed 2.5 when not supplied.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from ..enums import CandidateState, SignalAction
from ..interfaces.strategy import Strategy
from ..models import Candidate, MarketSnapshot, Signal


@dataclass
class VolatilityContractionConfig:
    max_contraction_ratio: float = 0.35   # price_range_pct_3m / price_range_pct_15m must be below this to count as "tight"
    min_broader_range_pct: float = 1.0    # price_range_pct_15m must be at least this -- excludes names that are just quiet everywhere, not contracting from a real move
    min_volume_acceleration: float = 2.0  # the volume kick confirming expansion out of the range
    max_spread_pct: float = 2.0


class VolatilityContractionBreakoutStrategy(Strategy):
    name = "volatility_contraction"
    version = "v1"

    def __init__(
        self,
        config: Optional[VolatilityContractionConfig] = None,
        *,
        reward_risk_ratio_fn: Callable[[], float] = lambda: 2.0,
        stop_loss_pct_fn: Callable[[], float] = lambda: 2.5,
    ):
        self.config = config or VolatilityContractionConfig()
        self._reward_risk_ratio_fn = reward_risk_ratio_fn
        self._stop_loss_pct_fn = stop_loss_pct_fn

    def on_snapshot(self, candidate: Candidate, snapshot: MarketSnapshot) -> Optional[Signal]:
        if candidate.state != CandidateState.ARMED:
            return None
        metrics = candidate.latest_metrics
        if metrics is None:
            return None

        if metrics.price_range_pct_15m < self.config.min_broader_range_pct:
            return None  # not enough recent range to be "contracting" from -- just generally quiet
        contraction_ratio = metrics.price_range_pct_3m / metrics.price_range_pct_15m
        if contraction_ratio > self.config.max_contraction_ratio:
            return None  # hasn't tightened enough recently

        if metrics.volume_accel_1m_3m < self.config.min_volume_acceleration:
            return None
        if metrics.price_velocity_1m <= 0:
            return None
        if metrics.spread_pct > self.config.max_spread_pct:
            return None

        entry_price = snapshot.last_price
        stop_price = entry_price * (1 - self._stop_loss_pct_fn() / 100.0)
        risk_per_share = entry_price - stop_price
        if risk_per_share <= 0:
            return None
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
            metadata={"contraction_ratio": contraction_ratio},
        )
