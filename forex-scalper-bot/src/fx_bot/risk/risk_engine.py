"""
Risk engine -- deliberately minimal in this Phase 2 skeleton. Its role
mirrors webull-momentum-bot/src/webull_bot/risk/risk_engine.py exactly:
a deterministic, auditable gate every ENTRY Signal must pass before it
becomes an Order, reproducible from backtest to live (no probabilistic/ML
logic). What's NOT here yet, on purpose: pip-based stop/target sizing,
max_spread_pips, session-window filters, daily-loss limits, correlated-
pair exposure caps -- the full field set the approved plan calls for.
Those belong to Phase 4 ("forex risk engine + position management"),
once there's a rule-builder/strategy config to actually drive them; adding
them now would mean designing for parameters nothing yet sets.

EXIT signals are never gated here -- see OrderManager.submit_signal,
which routes them straight to a closing order. Blocking an exit would
defeat the point of a risk engine (it exists to control new exposure, not
to trap you in a position).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from ..enums import SignalAction
from ..models import MarketSnapshot, Position, RiskDecision, Signal


@dataclass
class RiskConfig:
    stop_loss_required: bool = True
    max_simultaneous_positions: int = 1
    # A flat per-trade unit size -- real risk-%-of-equity/stop-distance
    # lot sizing (per the approved plan) is a Phase 4 concern; this is
    # just enough for the Phase 2 wiring proof to place a real order.
    default_quantity: float = 10_000.0


class RiskEngine:
    def __init__(self, config: Optional[RiskConfig] = None):
        self.config = config or RiskConfig()

    def evaluate(
        self,
        signal: Signal,
        *,
        open_positions: list[Position],
        snapshot: MarketSnapshot,
        now: Optional[datetime] = None,
    ) -> RiskDecision:
        if signal.action not in (SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT):
            return RiskDecision(approved=False, reason=f"evaluate() only gates entries, not {signal.action.value}")

        if self.config.stop_loss_required and signal.suggested_stop is None:
            return RiskDecision(approved=False, reason="Signal has no suggested_stop and stop_loss_required is set.")

        if len(open_positions) >= self.config.max_simultaneous_positions:
            return RiskDecision(approved=False, reason="max_simultaneous_positions reached.")

        return RiskDecision(approved=True, reason="approved", max_units=self.config.default_quantity)
