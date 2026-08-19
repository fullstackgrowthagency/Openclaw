"""
Tier 3: Trigger Engine.

Watches only ARMED candidates, using the same real-time snapshot stream, and
asks each configured Strategy whether entry conditions are met. This is
meant to run off streaming data rather than REST polling once the broker's
subscribe_quotes is wired up (see interfaces/broker.py).

Entry-selectivity rework (2026-08-13, see docs/ARCHITECTURE.md): a strategy
firing here used to move a candidate straight to TRIGGERED, which
TradingLoop then submitted an order for on that exact same tick -- off a
single snapshot, with no check that the move actually held for even a
second. Now it moves to CONFIRMING instead: the Signal is still returned
(so TradingLoop can stash it), but TradingLoop's confirmation-window logic
(_poll_confirmation) is what actually decides whether this ever becomes a
real order.

Real-Time Momentum Qualification Layer (2026-08-17, see
scanner/momentum_qualification.py): on_snapshot no longer transitions the
candidate to CONFIRMING itself -- it's a pure "which strategy matches"
function now, returning just the Signal (or None). TradingLoop is the sole
owner of the ARMED->CONFIRMING decision, since it now needs to run the
momentum-qualification gate on a fired Signal BEFORE deciding whether
CONFIRMING should start at all (a signal firing below the momentum regime,
or during an unhealthy pullback, must leave the candidate ARMED, not move
it to CONFIRMING and immediately fail it).
"""
from __future__ import annotations

from ..enums import CandidateState
from ..interfaces.strategy import Strategy
from ..models import Candidate, MarketSnapshot, Signal


class TriggerEngine:
    def __init__(self, strategies: list[Strategy]):
        self.strategies = strategies

    def on_snapshot(self, candidate: Candidate, snapshot: MarketSnapshot) -> Signal | None:
        if candidate.state != CandidateState.ARMED:
            return None

        for strategy in self.strategies:
            signal = strategy.on_snapshot(candidate, snapshot)
            if signal is not None:
                return signal
        return None
