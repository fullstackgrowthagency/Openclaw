"""
Momentum Regime strategy: fires ENTER_LONG directly off the same 5-minute
price-change threshold (metrics.return_5m) that
scanner/momentum_qualification.py's evaluate_trigger already hard-gates
ALL entries on downstream -- see that module's `min_return_5m_pct`
(scoring/rtms_weights.yaml, default 4.00%).

Added 2026-08-20 at explicit user request: every other strategy in this
package additionally requires some price-STRUCTURE condition (a breakout
level, a pullback, a VWAP reclaim, an opening range, volatility
contraction, ...) that a pure, fast vertical move doesn't necessarily
satisfy -- so a candidate could already be clearing RTMS's own regime bar
and still never generate a signal at all. This strategy has no structural
requirement; it exists purely to catch "this name is moving RIGHT NOW,"
independent of momentum_ignition_score.py's separate (and separately
weighted) momentum_regime_score MIS component -- that component only
ever influences the WATCHING->ARMED transition, never fires a signal by
itself.

Registered last in main.py's TriggerEngine list (most permissive of all
-- see that file's own ordering comment): a single-condition trigger this
broad would otherwise pre-empt every more selective pattern above it from
ever getting a chance to fire first.

See docs/ARCHITECTURE.md's "Entry strategies" section for the full
reasoning behind this and the other strategies registered alongside it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from ..enums import CandidateState, SignalAction
from ..interfaces.strategy import Strategy
from ..models import Candidate, MarketSnapshot, Signal


@dataclass
class MomentumRegimeConfig:
    # Same value as scoring/rtms_weights.yaml's min_return_5m_pct by
    # default -- NOT read from that file live (this codebase's
    # established convention: MIS/strategy thresholds and RTMS's own gate
    # are deliberately independent config surfaces, same tradeoff already
    # made in scoring/weights.yaml's momentum_regime_5m_exceptional
    # comment). Update both by hand together if one changes.
    min_return_5m_pct: float = 4.0
    max_spread_pct: float = 2.0


class MomentumRegimeStrategy(Strategy):
    name = "momentum_regime"
    version = "v1"

    def __init__(
        self,
        config: Optional[MomentumRegimeConfig] = None,
        *,
        reward_risk_ratio_fn: Callable[[], float] = lambda: 2.0,
        stop_loss_pct_fn: Callable[[], float] = lambda: 2.5,
    ):
        self.config = config or MomentumRegimeConfig()
        self._reward_risk_ratio_fn = reward_risk_ratio_fn
        self._stop_loss_pct_fn = stop_loss_pct_fn

    def on_snapshot(self, candidate: Candidate, snapshot: MarketSnapshot) -> Optional[Signal]:
        if candidate.state != CandidateState.ARMED:
            return None
        metrics = candidate.latest_metrics
        if metrics is None or metrics.return_5m is None:
            return None
        if metrics.return_5m < self.config.min_return_5m_pct:
            return None
        if metrics.spread_pct > self.config.max_spread_pct:
            return None

        entry_price = snapshot.last_price
        stop_price = entry_price * (1 - self._stop_loss_pct_fn() / 100.0)
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
            metadata={"return_5m": metrics.return_5m},
        )
